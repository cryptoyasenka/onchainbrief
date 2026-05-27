"""End-to-end demo run: a handful of REAL, diverse briefs via x402 on Base.

Earlier this demo took N recent Jupiter swaps — so every card was the same
event type and the feed's category tabs were all empty except one. This
version pulls recent signatures from several well-chosen Solana programs
(a DEX aggregator, an AMM, the upgradeable loader, SPL governance), decodes
each transaction into real facts via `txfacts.enrich_event`, and greedily
keeps briefs that land in *distinct* feed categories. The result is a feed
where Deployment / Volume / Governance tabs are actually populated, each
card reporting a concrete amount/asset/program rather than decoration.

Runs the full pipeline (SERP -> chat -> image, all settled on-chain via
x402 to USDC on Base), composes the card+brief, optionally attests on Solana
(if SOLANA_KEYPAIR_PATH is set), and rebuilds the static feed.

Bypasses the local-LLM triage filter and the daily throttle on purpose —
the goal is a deterministic demo for the bounty submission, not the
long-running watcher loop.

Cost: ~$0.11 per brief (3 ACE services x402-settled). DEMO_BRIEFS controls
how many (default 4 ~= $0.44). Confirms the production X402Client end-to-end.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import requests

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from onchainbrief.ace_brief_client import AceBriefClient  # noqa: E402
from onchainbrief.feed import build_feed  # noqa: E402
from onchainbrief.filter import categorize_event  # noqa: E402
from onchainbrief.pipeline import run_brief_async  # noqa: E402
from onchainbrief.txfacts import enrich_event, fetch_sol_price_usd  # noqa: E402
from onchainbrief.watcher import LogEvent  # noqa: E402
from onchainbrief.x402_client import X402Client  # noqa: E402

MAINNET_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
# getSignaturesForAddress + light calls hit MAINNET_RPC (OOBE Synapse in the
# hybrid setup — real ecosystem traffic). getTransaction needs an archival RPC
# OOBE's free tier won't serve, so decoding routes here instead.
DECODE_RPC = os.getenv("SOLANA_TX_RPC_URL", "https://solana-rpc.publicnode.com")
BRIEFS_DIR = os.getenv("BRIEFS_DIR", "./briefs")
SITE_HTML = os.getenv("SITE_HTML", "./site/index.html")
DEMO_BRIEFS = int(os.getenv("DEMO_BRIEFS", "4"))

# Sources chosen to span distinct feed categories. We scan recent signatures
# from each, decode them, and keep the ones that classify into *new* tabs.
# (label, program address) — ordered by how reliably each yields its category.
SOURCES: list[tuple[str, str]] = [
    ("BPF Upgradeable Loader (deploys/upgrades)", "BPFLoaderUpgradeab1e11111111111111111111111"),
    ("SPL Governance (proposals/votes)", "GovER5Lthms3bLBqWub97yVrMmEogzX7xNjdXpPPCVZw"),
    ("Jupiter v6 (swaps)", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"),
    ("Raydium AMM v4 (swaps/liquidity)", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"),
]
SIGS_PER_SOURCE = int(os.getenv("DEMO_SIGS_PER_SOURCE", "8"))


def fetch_recent_sigs(program: str, limit: int) -> list[str]:
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [program, {"limit": limit}],
    }
    try:
        r = requests.post(MAINNET_RPC, json=body, timeout=20)
        r.raise_for_status()
        res = r.json().get("result") or []
        # Skip failed transactions — a brief about a reverted tx is noise.
        return [it["signature"] for it in res if not it.get("err")]
    except (requests.RequestException, ValueError, KeyError):
        return []


def select_diverse_events(sol_price: float | None) -> list[LogEvent]:
    """Greedily pick decoded events spanning as many categories as possible."""
    chosen: list[LogEvent] = []
    seen_cats: set[str] = set()
    seen_sigs: set[str] = set()

    # Pass 1: one event per source that lands in a brand-new category.
    pending: list[LogEvent] = []
    for label, addr in SOURCES:
        sigs = fetch_recent_sigs(addr, SIGS_PER_SOURCE)
        print(f"[SCAN] {label}: {len(sigs)} sigs")
        picked_here = False
        for sig in sigs:
            if sig in seen_sigs:
                continue
            ev = LogEvent(signature=sig, logs=[], program_ids=[addr])
            enrich_event(ev, MAINNET_RPC, tx_rpc_url=DECODE_RPC, sol_price_usd=sol_price)
            cat = categorize_event(ev)
            kind = getattr(getattr(ev, "facts", None), "kind", "?")
            if not picked_here and cat not in seen_cats:
                chosen.append(ev)
                seen_cats.add(cat)
                seen_sigs.add(sig)
                picked_here = True
                print(f"  + {sig[:12]}… kind={kind} -> {cat} (new tab)")
                if len(chosen) >= DEMO_BRIEFS:
                    return chosen
            else:
                pending.append(ev)
                seen_sigs.add(sig)

    # Pass 2: backfill toward DEMO_BRIEFS, but spread across categories instead
    # of taking list order — otherwise a prolific source (the loader emits many
    # deploys) floods the feed with near-identical cards, the very repetition
    # this rewrite set out to kill.
    from collections import Counter

    cat_counts = Counter(categorize_event(c) for c in chosen)
    chosen_sigs = {c.signature for c in chosen}
    remaining = [e for e in pending if e.signature not in chosen_sigs]
    while remaining and len(chosen) < DEMO_BRIEFS:
        # Pick the pending event whose category is currently least represented.
        ev = min(remaining, key=lambda e: cat_counts[categorize_event(e)])
        remaining.remove(ev)
        cat = categorize_event(ev)
        cat_counts[cat] += 1
        chosen.append(ev)
        print(f"  + {ev.signature[:12]}… -> {cat} (backfill)")
    return chosen


def build_attestor():
    kp = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if not kp:
        print("[ATTEST] disabled (SOLANA_KEYPAIR_PATH not set)")
        return None
    cluster = os.getenv("ATTEST_CLUSTER", "devnet")
    rpc = os.getenv(
        "ATTEST_RPC_URL",
        "https://api.devnet.solana.com"
        if cluster == "devnet"
        else "https://api.mainnet-beta.solana.com",
    )
    from onchainbrief.attest import Attestor
    try:
        a = Attestor(rpc, kp, cluster=cluster)
        print(f"[ATTEST] enabled cluster={cluster}")
        return a
    except Exception as e:
        print(f"[ATTEST] disabled (keypair load failed): {e!r}")
        return None


async def main() -> int:
    if not os.getenv("ACE_X402_PRIVATE_KEY"):
        print("BLOCKED: ACE_X402_PRIVATE_KEY not set in .env")
        return 2

    x402 = X402Client()
    print(f"[X402] pay address: {x402.pay_address}")
    client = AceBriefClient(x402)
    attestor = build_attestor()

    sol_price = fetch_sol_price_usd()
    print(f"[PRICE] SOL/USD = {sol_price if sol_price else 'unavailable (USD omitted)'}")

    events = select_diverse_events(sol_price)
    if not events:
        print("BLOCKED: no decodable events found across sources")
        return 3
    cats = ", ".join(sorted({categorize_event(e) for e in events}))
    print(f"[EVENTS] {len(events)} briefs across categories: {cats}")

    for i, ev in enumerate(events, 1):
        print(f"\n=== brief {i}/{len(events)} — sig={ev.signature[:16]}… ===")
        art = await run_brief_async(ev, client, BRIEFS_DIR, attestor=attestor)
        att = f" attest={art.attestation.tx_sig[:16]}…" if art.attestation else ""
        print(f"[BRIEF] {art.headline} -> {art.card_path}{att}")
        print(f"  brief: {art.brief_path}")

    out = build_feed(BRIEFS_DIR, SITE_HTML)
    print(f"\n[FEED] rebuilt -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
