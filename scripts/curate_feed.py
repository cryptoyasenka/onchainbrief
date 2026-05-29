"""Curate the feed on SIGNIFICANT events (significance_score, MIN_BRIEF_USD).

Scans recent signatures from the swap venues (and optionally governance/loader),
decodes each via txfacts, scores it with filter.significance_score, and ranks.

  --dry   scan + rank + print the eligible candidates, SPEND NOTHING.
  (default) generate briefs for the top TARGET_BRIEFS candidates via the paid
            x402 pipeline + mainnet attestation, then rebuild the feed.

The point: a sub-threshold ($0.11) swap can never headline the feed again. With
the stable/SOL-leg valuation in txfacts, a big memecoin swap is now visible by
its dollar size, so the Volume tab leads with a real move.

Cost (non-dry): ~$0.11 x402 per brief + a mainnet memo fee (~5e-6 SOL) each.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from onchainbrief.ace_brief_client import AceBriefClient  # noqa: E402
from onchainbrief.attest import Attestor  # noqa: E402
from onchainbrief.feed import build_feed  # noqa: E402
from onchainbrief.filter import (  # noqa: E402
    MIN_BRIEF_USD,
    categorize_event,
    is_significant,
    significance_score,
)
from onchainbrief.pipeline import run_brief_async  # noqa: E402
from onchainbrief.txfacts import enrich_event, fetch_sol_price_usd  # noqa: E402
from onchainbrief.watcher import LogEvent  # noqa: E402
from onchainbrief.x402_client import X402Client  # noqa: E402

MAIN = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
DECODE = os.getenv("SOLANA_TX_RPC_URL", "https://solana-rpc.publicnode.com")
BRIEFS_DIR = str(ROOT / "briefs")
SITE_HTML = str(ROOT / "site" / "index.html")
SIGS_PER_SOURCE = int(os.getenv("CURATE_SIGS_PER_SOURCE", "60"))
TARGET_BRIEFS = int(os.getenv("CURATE_TARGET", "2"))
# A shallow recent window (one RPC page ~= seconds of activity) tops out around
# a few $k. Whale swaps (>=$100k) need walking back: paginate sigs with a
# `before` cursor up to MAX_SIGS, decoding until ENOUGH candidates clear the bar.
MAX_SIGS = int(os.getenv("CURATE_MAX_SIGS", "1500"))
ENOUGH = int(os.getenv("CURATE_ENOUGH", str(max(6, TARGET_BRIEFS * 3))))

# Swap venues lead (the weak cards we are replacing are swaps); governance and
# the loader are eligible too since those kinds keep by nature.
SOURCES: list[tuple[str, str]] = [
    ("Jupiter v6", "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"),
    ("Raydium AMM v4", "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"),
    ("Orca Whirlpool", "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc"),
]


def _sig_page(program: str, before: str | None, limit: int) -> list[dict]:
    params: dict = {"limit": limit}
    if before:
        params["before"] = before
    body = {"jsonrpc": "2.0", "id": 1,
            "method": "getSignaturesForAddress", "params": [program, params]}
    try:
        # List from the archival RPC, not OOBE staging (which caps the result
        # set to a handful of sigs - too small a window to surface a whale swap).
        r = requests.post(DECODE, json=body, timeout=25)
        r.raise_for_status()
        return r.json().get("result") or []
    except (requests.RequestException, ValueError, KeyError):
        return []


def recent_sigs(program: str, max_sigs: int) -> list[str]:
    """Walk back getSignaturesForAddress pages (cursor=`before`) up to max_sigs."""
    out: list[str] = []
    before: str | None = None
    while len(out) < max_sigs:
        page = _sig_page(program, before, min(1000, max_sigs - len(out)))
        if not page:
            break
        out.extend(it["signature"] for it in page if not it.get("err"))
        before = page[-1]["signature"]
        if len(page) < 1000:
            break  # reached the tip of available history
    return out


def scan(sol_price: float | None) -> list[LogEvent]:
    """Decode + score sigs across sources; early-stop once ENOUGH clear the bar."""
    seen: set[str] = set()
    eligible: list[LogEvent] = []
    decoded = 0
    for label, addr in SOURCES:
        sigs = recent_sigs(addr, MAX_SIGS)
        print(f"[SCAN] {label}: {len(sigs)} sigs (decoding until {ENOUGH} clear bar)")
        for sig in sigs:
            if sig in seen:
                continue
            seen.add(sig)
            ev = LogEvent(signature=sig, logs=[], program_ids=[addr])
            try:
                enrich_event(ev, MAIN, tx_rpc_url=DECODE, sol_price_usd=sol_price)
            except Exception:
                continue
            decoded += 1
            if is_significant(ev):
                eligible.append(ev)
                f = ev.facts
                print(f"  HIT {significance_score(ev):.2f} {categorize_event(ev)} "
                      f"usd={f.amount_usd} {f.asset} {sig[:16]}… (decoded {decoded})")
            if len(eligible) >= ENOUGH:
                break
        if len(eligible) >= ENOUGH:
            break
    print(f"[SCAN] decoded {decoded} txs, {len(eligible)} eligible")
    eligible.sort(key=significance_score, reverse=True)
    return eligible


def print_candidates(events: list[LogEvent]) -> None:
    print(f"\n[CANDIDATES] {len(events)} eligible (bar = ${MIN_BRIEF_USD:,.0f}):")
    for ev in events[:15]:
        f = ev.facts
        print(f"  {significance_score(ev):6.2f}  {categorize_event(ev):11s} "
              f"usd={f.amount_usd!r:>14} {f.asset:>8}  {ev.signature[:20]}…")


def build_attestor() -> Attestor:
    kp = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if not kp or not os.path.exists(os.path.expandvars(os.path.expanduser(kp))):
        raise SystemExit("BLOCKED: SOLANA_KEYPAIR_PATH missing")
    if os.getenv("ATTEST_CLUSTER") != "mainnet-beta":
        raise SystemExit(f"BLOCKED: ATTEST_CLUSTER={os.getenv('ATTEST_CLUSTER')!r}, want mainnet-beta")
    return Attestor(os.getenv("ATTEST_RPC_URL"), kp, cluster="mainnet-beta")


async def main() -> int:
    dry = "--dry" in sys.argv
    sol_price = fetch_sol_price_usd()
    print(f"[PRICE] SOL/USD = {sol_price}")
    events = scan(sol_price)
    print_candidates(events)
    if not events:
        print("BLOCKED: no significant events found — widen CURATE_SIGS_PER_SOURCE or sources")
        return 3
    if dry:
        print("\n[DRY] no spend. Re-run without --dry to generate the top "
              f"{TARGET_BRIEFS} brief(s).")
        return 0

    if not os.getenv("ACE_X402_PRIVATE_KEY"):
        print("BLOCKED: ACE_X402_PRIVATE_KEY not set")
        return 2
    attestor = build_attestor()
    x402 = X402Client()
    print(f"[X402] pay address: {x402.pay_address}")
    client = AceBriefClient(x402)

    for i, ev in enumerate(events[:TARGET_BRIEFS], 1):
        f = ev.facts
        print(f"\n=== brief {i}/{TARGET_BRIEFS} {ev.signature[:16]}… "
              f"{categorize_event(ev)} usd={f.amount_usd} ===")
        art = await run_brief_async(ev, client, BRIEFS_DIR, attestor=attestor)
        att = art.attestation.tx_sig if art.attestation else "(none)"
        print(f"[BRIEF] {art.headline}\n  card: {art.card_path}\n  attest: {att}")

    out = build_feed(BRIEFS_DIR, SITE_HTML)
    print(f"\n[FEED] rebuilt -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
