"""End-to-end demo run: 2 real briefs via x402 on Base mainnet.

Picks 2 recent transactions from Jupiter v6 aggregator on Solana mainnet,
treats each as a `LogEvent`, runs the full pipeline (SERP -> chat -> image,
all settled on-chain via x402 to USDC on Base), composes the card+brief,
optionally attests on Solana (if SOLANA_KEYPAIR_PATH is set), and rebuilds
the static feed.

Bypasses the local-LLM triage filter (the heuristic) and the daily throttle
on purpose — the goal is a deterministic 1-2-brief demo for the bounty
submission, not the long-running watcher loop.

Cost: ~$0.11 per brief (3 ACE services x402-settled). Confirms the
production X402Client end-to-end on real ACE endpoints.
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
from onchainbrief.pipeline import run_brief_async  # noqa: E402
from onchainbrief.watcher import LogEvent  # noqa: E402
from onchainbrief.x402_client import X402Client  # noqa: E402

JUPITER_V6 = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"
MAINNET_RPC = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
BRIEFS_DIR = os.getenv("BRIEFS_DIR", "./briefs")
SITE_HTML = os.getenv("SITE_HTML", "./site/index.html")


def fetch_recent_sigs(program: str, limit: int = 2) -> list[str]:
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [program, {"limit": limit}],
    }
    r = requests.post(MAINNET_RPC, json=body, timeout=20)
    r.raise_for_status()
    return [it["signature"] for it in r.json()["result"]]


def synth_event(sig: str) -> LogEvent:
    # We don't have real log_messages without a getTransaction roundtrip
    # (and the pipeline only uses signature + program_ids[0] for headline
    # + SERP query). Keep it minimal.
    return LogEvent(
        signature=sig,
        logs=[f"Program {JUPITER_V6} invoke [1]", "Program log: swap"] * 4,
        program_ids=[JUPITER_V6],
    )


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

    sigs = fetch_recent_sigs(JUPITER_V6, limit=2)
    print(f"[EVENTS] {len(sigs)} recent Jupiter v6 sigs:")
    for s in sigs:
        print(f"  - {s}")

    for i, sig in enumerate(sigs, 1):
        print(f"\n=== brief {i}/{len(sigs)} — sig={sig[:16]}... ===")
        ev = synth_event(sig)
        art = await run_brief_async(ev, client, BRIEFS_DIR, attestor=attestor)
        att = f" attest={art.attestation.tx_sig[:16]}..." if art.attestation else ""
        print(f"[BRIEF] {art.headline} -> {art.card_path}{att}")
        print(f"  brief: {art.brief_path}")

    out = build_feed(BRIEFS_DIR, SITE_HTML)
    print(f"\n[FEED] rebuilt -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
