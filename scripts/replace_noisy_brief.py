"""Surgically replace ONE noisy launch brief with a clean Volume card.

The 467seNkt… card reported an unresolved SPL mint as its asset ("Tqj8…")
and the old image prompt baked that garbled text into the art. Its PNG+MD are
hashed into an on-chain memo, so they can't be edited — the honest fix is a
fresh brief on a clean event (resolvable ticker) plus a new attestation, then
dropping the old card.

This picks a recent Jupiter swap whose asset resolves to a real ticker
(USDC/USDT/…) with a USD value, runs the full paid pipeline ONCE (SERP+chat+
image via x402 on Base), attests on Solana mainnet, and rebuilds the feed. It
does NOT touch the other three launch briefs.

Cost: ~$0.11 x402 (3 ACE services) + a mainnet memo fee (~0.000005 SOL).
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import re
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from onchainbrief.ace_brief_client import AceBriefClient  # noqa: E402
from onchainbrief.attest import Attestor  # noqa: E402
from onchainbrief.feed import build_feed  # noqa: E402
from onchainbrief.filter import categorize_event  # noqa: E402
from onchainbrief.pipeline import run_brief_async  # noqa: E402
from onchainbrief.txfacts import enrich_event, fetch_sol_price_usd  # noqa: E402
from onchainbrief.watcher import LogEvent  # noqa: E402
from onchainbrief.x402_client import X402Client  # noqa: E402

MAIN = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
DECODE = os.getenv("SOLANA_TX_RPC_URL", "https://solana-rpc.publicnode.com")
BRIEFS_DIR = str(ROOT / "briefs")
SITE_HTML = str(ROOT / "site" / "index.html")
JUP = "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"

# A clean ticker: short, alphabetic, not the native SOL (covered elsewhere),
# and definitely not a truncated base58 mint (those carry a trailing "…").
_TICKER = re.compile(r"[A-Za-z]{2,8}")


def _clean_asset(a: str) -> bool:
    return bool(a) and a != "SOL" and _TICKER.fullmatch(a) is not None


def _recent_sigs(program: str, limit: int) -> list[str]:
    body = {
        "jsonrpc": "2.0", "id": 1,
        "method": "getSignaturesForAddress",
        "params": [program, {"limit": limit}],
    }
    r = requests.post(MAIN, json=body, timeout=20)
    r.raise_for_status()
    return [it["signature"] for it in (r.json().get("result") or []) if not it.get("err")]


def pick_clean_volume_event(sol_price: float | None) -> LogEvent | None:
    """Best = largest USD swap with a resolvable ticker among recent Jupiter txs."""
    best: tuple[float, LogEvent] | None = None
    for sig in _recent_sigs(JUP, 20):
        ev = LogEvent(signature=sig, logs=[], program_ids=[JUP])
        try:
            enrich_event(ev, MAIN, tx_rpc_url=DECODE, sol_price_usd=sol_price)
        except Exception:
            continue
        f = getattr(ev, "facts", None)
        if f is None or categorize_event(ev) != "Volume":
            continue
        if not getattr(f, "has_value", False) or not _clean_asset(getattr(f, "asset", "") or ""):
            continue
        usd = getattr(f, "amount_usd", None) or 0.0
        if best is None or usd > best[0]:
            best = (usd, ev)
    return best[1] if best else None


async def main() -> int:
    if not os.getenv("ACE_X402_PRIVATE_KEY"):
        print("BLOCKED: ACE_X402_PRIVATE_KEY not set")
        return 2
    if os.getenv("ATTEST_CLUSTER") != "mainnet-beta":
        print(f"BLOCKED: ATTEST_CLUSTER={os.getenv('ATTEST_CLUSTER')!r}, expected mainnet-beta")
        return 2
    kp = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if not kp or not os.path.exists(os.path.expandvars(os.path.expanduser(kp))):
        print("BLOCKED: SOLANA_KEYPAIR_PATH missing")
        return 2

    sol_price = fetch_sol_price_usd()
    print(f"[PRICE] SOL/USD = {sol_price}")
    ev = pick_clean_volume_event(sol_price)
    if ev is None:
        print("BLOCKED: no clean Volume swap found in recent Jupiter txs")
        return 3
    f = ev.facts
    print(f"[PICK] {ev.signature}")
    print(f"       asset={f.asset} amount={f.amount_native} usd={f.amount_usd} instr={getattr(f, 'instruction', '')}")

    x402 = X402Client()
    print(f"[X402] pay address: {x402.pay_address}")
    client = AceBriefClient(x402)
    attestor = Attestor(os.getenv("ATTEST_RPC_URL"), kp, cluster="mainnet-beta")
    print("[ATTEST] cluster=mainnet-beta")

    art = await run_brief_async(ev, client, BRIEFS_DIR, attestor=attestor)
    att = art.attestation.tx_sig if art.attestation else "(none)"
    print(f"\n[BRIEF] {art.headline}")
    print(f"  card:   {art.card_path}")
    print(f"  brief:  {art.brief_path}")
    print(f"  attest: {att}")

    out = build_feed(BRIEFS_DIR, SITE_HTML)
    print(f"[FEED] rebuilt -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
