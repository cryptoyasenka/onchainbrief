"""Pull recent USDC Transfer tx hashes from wallet to ACE facilitator on Base.

Bundle proof for the e2e demo briefs (or any prior x402 burn within the
queried block range). Reads BASE_RPC_URL from env or falls back to a known
public RPC.
"""

from __future__ import annotations

import json
import os
import sys

import requests

WALLET = "0x93A3A523FE54E0dF382BA6B390069225605c58A9"
FACILITATOR = "0x4F0E2D3477a1B94CF33d16E442CEe4733dadCeE7"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

RPC = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")


def call(method: str, params: list) -> dict:
    r = requests.post(RPC, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=30)
    r.raise_for_status()
    j = r.json()
    if "error" in j:
        raise RuntimeError(j["error"])
    return j["result"]


def pad_addr(a: str) -> str:
    return "0x" + a.lower().removeprefix("0x").zfill(64)


def main() -> int:
    lookback = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
    latest = int(call("eth_blockNumber", []), 16)
    from_block = latest - lookback
    print(f"Querying Base blocks {from_block}..{latest} (~{lookback * 2 / 60:.1f} min)")

    logs = call("eth_getLogs", [{
        "fromBlock": hex(from_block),
        "toBlock": hex(latest),
        "address": USDC,
        "topics": [TRANSFER_SIG, pad_addr(WALLET), pad_addr(FACILITATOR)],
    }])

    print(f"\nFound {len(logs)} USDC Transfer(wallet -> facilitator) events:\n")
    total = 0
    for lg in logs:
        amt = int(lg["data"], 16)
        total += amt
        blk = int(lg["blockNumber"], 16)
        tx = lg["transactionHash"]
        print(f"  block={blk}  amount={amt / 1e6:.6f} USDC  tx={tx}")
        print(f"    https://basescan.org/tx/{tx}")

    print(f"\nTotal: {total / 1e6:.6f} USDC across {len(logs)} transfers")

    bal_raw = call("eth_call", [{
        "to": USDC,
        "data": "0x70a08231" + pad_addr(WALLET)[2:],
    }, "latest"])
    print(f"\nCurrent wallet USDC balance: {int(bal_raw, 16) / 1e6:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
