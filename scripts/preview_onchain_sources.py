"""READ-ONLY preview for the "on-chain sources" idea (option A).

For each deploy/upgrade brief, resolve from the chain:
  - the program account that was deployed/upgraded (from the loader's own log)
  - that program's upgrade authority (program -> programData -> info.authority)
and print the source list option A would attach instead of the generic
topic articles all deploy cards currently share.

NO writes, NO attestation, NO publish, NO USDC. Just getTransaction +
getAccountInfo against the public RPC. Run:
    .venv/Scripts/python.exe scripts/preview_onchain_sources.py
"""

from __future__ import annotations

import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import requests  # noqa: E402

try:
    from dotenv import load_dotenv  # noqa: E402

    load_dotenv(ROOT / ".env")
except Exception:
    pass

from onchainbrief.txfacts import _deploy_target, fetch_transaction  # noqa: E402

RPC = os.getenv("MAINNET_RPC_URL") or "https://api.mainnet.solana.com"
DOC = "https://solana.com/docs/core/programs/program-deployment"

# (brief id, trigger sig) — the 3 deploy/upgrade cards. Governance brief omitted:
# it already has a distinct, topic-correct source set.
DEPLOYS: list[tuple[str, str]] = [
    ("65PNPvjZgGRwPFne",
     "65PNPvjZgGRwPFneFnZZpjoLmqhRyw1qPPvPgBjuvzZMXmDTzmk3BKqFhZXGxe4KuFbf84ogBwLpiR5F3ejt1f9x"),
    ("2mdJjk7WbnYpcDE2",
     "2mdJjk7WbnYpcDE2vj3GPTNdDY3SmBLs9o7s6mooDdT1cHsBUSqCQB3WBs4fWijragYqjruRzdYRLV5gQPndMyMa"),
    ("2YxfWHLxbs4hya1k",
     "2YxfWHLxbs4hya1kYaQSDRJdu528YfUFxgr3L7hiJWax7Drx2FMU1KpAi4c6ajjoufGtHXwHUALuYE3k2UmdsBfy"),
]


def _account_info_parsed(addr: str) -> dict | None:
    """getAccountInfo(jsonParsed) -> the `parsed` dict, or None."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getAccountInfo",
        "params": [addr, {"encoding": "jsonParsed"}],
    }
    try:
        r = requests.post(RPC, json=body, timeout=20)
        r.raise_for_status()
        val = (r.json().get("result") or {}).get("value") or {}
        data = val.get("data")
        if isinstance(data, dict):
            return data.get("parsed")
    except Exception as e:  # noqa: BLE001
        print(f"      (getAccountInfo error: {e})")
    return None


def _upgrade_authority(program_id: str) -> tuple[str | None, str]:
    """Return (authority_or_None, note). program -> programData -> info.authority."""
    parsed = _account_info_parsed(program_id)
    if not parsed or parsed.get("type") != "program":
        return None, "not an upgradeable-loader program account (or not found)"
    pdata = (parsed.get("info") or {}).get("programData")
    if not pdata:
        return None, "no programData pointer"
    pdparsed = _account_info_parsed(pdata)
    if not pdparsed or pdparsed.get("type") != "programData":
        return None, f"programData {pdata[:8]}… not parseable"
    auth = (pdparsed.get("info") or {}).get("authority")
    if auth is None:
        return None, f"authority REVOKED (immutable); programData {pdata[:8]}…"
    return auth, f"programData {pdata[:8]}…"


def main() -> int:
    print(f"RPC: {RPC}\n")
    for bid, trigger in DEPLOYS:
        print(f"=== {bid} ===")
        result = fetch_transaction(trigger, RPC)
        if not result:
            print("  getTransaction returned nothing\n")
            continue
        dt = _deploy_target(result)
        if not dt:
            print("  program-id NOT in loader logs — fallback anchor = the tx itself")
            print(f"  proposed sources:\n    - https://solscan.io/tx/{trigger}\n    - {DOC}\n")
            continue
        verb, pid = dt
        print(f"  {verb} program: {pid}")
        auth, note = _upgrade_authority(pid)
        print(f"  upgrade authority: {auth or '(none / revoked)'}  [{note}]")
        srcs = [f"https://solscan.io/account/{pid}"]
        if auth:
            srcs.append(f"https://solscan.io/account/{auth}")
        srcs.append(DOC)
        print("  proposed sources (option A):")
        for s in srcs:
            print(f"    - {s}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
