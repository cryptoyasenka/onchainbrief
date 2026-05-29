"""Targeted paid re-curation: generate exactly 1 Governance + 1 Deployment brief.

The whale-swap wall (no swap >= $100k reachable by walking program sigs) means
the two weak swap cards can't be replaced by a bigger swap. The significance
gate keeps deploy/governance by NATURE regardless of dollars, so we re-curate
the feed with those instead (operator-selected mix: 1 governance + 1 deploy).

Scans the upgradeable loader + SPL Governance, decodes via txfacts, picks the
single highest-scoring governance event and the strongest deploy (an UPGRADE
that names a concrete program beats an anonymous fresh deploy), prints both
picks, then runs the paid x402 pipeline + mainnet attestation on each.

Does NOT delete the old swap cards and does NOT rebuild the feed - that is done
by hand after a quality check, so a bad generation never destroys the feed.

Cost: ~$0.11 x402 per brief (2 briefs ~= $0.22) + a mainnet memo fee each.
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
from onchainbrief.filter import (  # noqa: E402
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
PER = int(os.getenv("CURATE_SIGS_PER_SOURCE", "30"))

LOADER = "BPFLoaderUpgradeab1e11111111111111111111111"
GOV = "GovER5Lthms3bLBqWub97yVrMmEogzX7xNjdXpPPCVZw"
SOURCES = [("BPF Upgradeable Loader", LOADER), ("SPL Governance", GOV)]


def _sigs(addr: str, limit: int) -> list[str]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
            "params": [addr, {"limit": limit}]}
    try:
        r = requests.post(DECODE, json=body, timeout=25)
        r.raise_for_status()
        return [it["signature"] for it in (r.json().get("result") or []) if not it.get("err")]
    except (requests.RequestException, ValueError, KeyError):
        return []


def scan(sol_price: float | None) -> list[LogEvent]:
    seen: set[str] = set()
    out: list[LogEvent] = []
    for label, addr in SOURCES:
        ss = _sigs(addr, PER)
        print(f"[SCAN] {label}: {len(ss)} ok sigs")
        for s in ss:
            if s in seen:
                continue
            seen.add(s)
            ev = LogEvent(signature=s, logs=[], program_ids=[addr])
            try:
                enrich_event(ev, MAIN, tx_rpc_url=DECODE, sol_price_usd=sol_price)
            except Exception:
                continue
            if is_significant(ev):
                out.append(ev)
    return out


def _is_upgrade(ev: LogEvent) -> bool:
    return "upgrad" in (getattr(ev.facts, "deploy_verb", "") or "").lower()


def pick(events: list[LogEvent]) -> tuple[LogEvent | None, LogEvent | None]:
    gov = sorted(
        (e for e in events if categorize_event(e) == "Governance"),
        key=significance_score, reverse=True,
    )
    dep = sorted(
        (e for e in events if categorize_event(e) == "Deployment"),
        # upgrades (named program) first, then by score
        key=lambda e: (_is_upgrade(e), significance_score(e)), reverse=True,
    )
    return (gov[0] if gov else None), (dep[0] if dep else None)


def _show(tag: str, ev: LogEvent) -> None:
    f = ev.facts
    print(f"  [{tag}] {categorize_event(ev):11s} score={significance_score(ev):.2f} "
          f"prog={getattr(f,'program_name','') or getattr(f,'program_id','') or '-'} "
          f"verb={getattr(f,'deploy_verb','') or '-'} usd={getattr(f,'amount_usd',None)}\n"
          f"        sig={ev.signature}")


async def main() -> int:
    sol_price = fetch_sol_price_usd()
    print(f"[PRICE] SOL/USD={sol_price}")
    events = scan(sol_price)
    print(f"[SCAN] {len(events)} eligible "
          f"({sum(categorize_event(e)=='Governance' for e in events)} gov, "
          f"{sum(categorize_event(e)=='Deployment' for e in events)} deploy)")
    gov, dep = pick(events)
    if not gov or not dep:
        print(f"BLOCKED: need 1 gov + 1 deploy, got gov={bool(gov)} deploy={bool(dep)}")
        return 3
    print("\n[PICKS]")
    _show("GOV", gov)
    _show("DEP", dep)

    if not os.getenv("ACE_X402_PRIVATE_KEY"):
        print("BLOCKED: ACE_X402_PRIVATE_KEY not set")
        return 2
    kp = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if not kp or not os.path.exists(os.path.expandvars(os.path.expanduser(kp))):
        print("BLOCKED: SOLANA_KEYPAIR_PATH missing")
        return 2
    if os.getenv("ATTEST_CLUSTER") != "mainnet-beta":
        print(f"BLOCKED: ATTEST_CLUSTER={os.getenv('ATTEST_CLUSTER')!r}, want mainnet-beta")
        return 2
    attestor = Attestor(os.getenv("ATTEST_RPC_URL"), kp, cluster="mainnet-beta")
    x402 = X402Client()
    print(f"\n[X402] pay address: {x402.pay_address}")
    client = AceBriefClient(x402)

    for tag, ev in (("GOV", gov), ("DEP", dep)):
        print(f"\n=== {tag} brief {ev.signature[:16]}… ===")
        art = await run_brief_async(ev, client, BRIEFS_DIR, attestor=attestor)
        att = art.attestation.tx_sig if art.attestation else "(none)"
        print(f"[BRIEF] {art.headline}\n  card:   {art.card_path}\n  brief:  {art.brief_path}\n  attest: {att}")
    print("\n[DONE] 2 briefs generated. Feed NOT rebuilt yet (quality check first).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
