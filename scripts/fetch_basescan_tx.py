"""Verify the x402 USDC settlements behind the demo briefs on Base mainnet.

Default (no args): re-fetch the canonical per-service settlement transactions by
hash and confirm each is a real USDC Transfer from the agent wallet to the ACE
facilitator. Deterministic — it works no matter how old the blocks are, so the
proof is reproducible long after the burn.

Optional (`python scripts/fetch_basescan_tx.py <lookback_blocks>`): scan the most
recent <lookback_blocks> Base blocks for USDC Transfer(wallet -> facilitator)
logs — handy right after a fresh burn to bundle whatever just settled.

Reads BASE_RPC_URL from env or falls back to a known public RPC.
"""

from __future__ import annotations

import os
import sys

import requests

WALLET = "0x93A3A523FE54E0dF382BA6B390069225605c58A9"
FACILITATOR = "0x4F0E2D3477a1B94CF33d16E442CEe4733dadCeE7"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# Canonical per-service x402 settlements (Base mainnet) from the documented probe
# run — one USDC Transfer per ACE service. These are public on-chain txs, so the
# payment path stays independently verifiable here or on basescan at any time.
SETTLEMENTS = [
    ("chat  (gpt-4o-mini)", "0x9ef457e91d42b36567cccad85c1698b054a83840c069de658eb35681a92c1966"),
    ("serp  (google)", "0xeca3004579e938b1a7b42cb950b7e4d73069c5a8f0670c51dd28ac2c37e3c3a2"),
    ("image (nano-banana)", "0xfcce606400012ef1ec03b3c8b13839074f6f9effa19098d2f5ddf5693681632f"),
]

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


def _transfer_amount(log: dict) -> int | None:
    """Return the USDC amount if `log` is a Transfer(wallet -> facilitator)."""
    topics = log.get("topics", [])
    if (
        log.get("address", "").lower() == USDC.lower()
        and len(topics) == 3
        and topics[0].lower() == TRANSFER_SIG
        and "0x" + topics[1][-40:].lower() == WALLET.lower()
        and "0x" + topics[2][-40:].lower() == FACILITATOR.lower()
    ):
        return int(log["data"], 16)
    return None


def verify_settlements() -> int:
    print(
        f"Verifying {len(SETTLEMENTS)} x402 settlements by tx hash "
        f"(USDC {WALLET[:8]}... -> ACE facilitator {FACILITATOR[:8]}...) on Base mainnet:\n"
    )
    total = 0
    ok = 0
    for label, tx in SETTLEMENTS:
        rcpt = call("eth_getTransactionReceipt", [tx])
        amt = None
        for lg in (rcpt or {}).get("logs", []):
            amt = _transfer_amount(lg)
            if amt is not None:
                break
        if rcpt and rcpt.get("status") == "0x1" and amt is not None:
            ok += 1
            total += amt
            print(f"  [OK]   {label:20s} {amt / 1e6:.6f} USDC  tx={tx}")
            print(f"         https://basescan.org/tx/{tx}")
        else:
            print(f"  [FAIL] {label:20s} {tx}  (no matching USDC transfer)")
    print(f"\n{ok}/{len(SETTLEMENTS)} verified -- total {total / 1e6:.6f} USDC settled to ACE.")
    bal_raw = call("eth_call", [{"to": USDC, "data": "0x70a08231" + pad_addr(WALLET)[2:]}, "latest"])
    print(f"Current wallet USDC balance: {int(bal_raw, 16) / 1e6:.6f}")
    return 0 if ok == len(SETTLEMENTS) else 1


def scan_blocks(lookback: int) -> int:
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
    bal_raw = call("eth_call", [{"to": USDC, "data": "0x70a08231" + pad_addr(WALLET)[2:]}, "latest"])
    print(f"\nCurrent wallet USDC balance: {int(bal_raw, 16) / 1e6:.6f}")
    return 0


def main() -> int:
    # A numeric arg switches to the recent-block scan; default verifies the
    # canonical settlements by hash so the reproducer never returns an empty set.
    if len(sys.argv) > 1:
        return scan_blocks(int(sys.argv[1]))
    return verify_settlements()


if __name__ == "__main__":
    raise SystemExit(main())
