"""Empirical devnet proof of the P4a attestation path.

Airdrops a small amount of SOL to the dedicated keypair on Solana devnet
(free) and sends one real memo-program tx via the same Attestor that the
runtime uses in production. Prints the explorer link so anyone can verify
the receipt landed on-chain.

This is the п.1 (synthetic-tests) + п.5 (real-docs, not assumed) gate for
P4a — proves the code path works before any mainnet SOL is spent.

Usage:  python scripts/attest_devnet_check.py
        SOLANA_KEYPAIR_PATH must point at a Solana CLI json-array key.
"""

from __future__ import annotations

import os
import pathlib
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from solana.rpc.api import Client  # noqa: E402
from solders.pubkey import Pubkey  # noqa: E402

from onchainbrief.attest import Attestor, explorer_url  # noqa: E402

DEVNET_RPC = "https://api.devnet.solana.com"
AIRDROP_LAMPORTS = 100_000_000  # 0.1 SOL — plenty for many memo txs


def _ensure_balance(client: Client, pubkey: Pubkey, want: int) -> int:
    """Top up via devnet faucet if balance is below `want` lamports.

    Public devnet faucet is rate-limited and often returns "Internal error".
    Retries 3x with backoff; on persistent failure, points the operator at
    the web faucet for a manual one-shot.
    """
    bal = client.get_balance(pubkey).value
    if bal >= want:
        print(f"[balance] {bal} lamports — sufficient, skipping airdrop")
        return bal

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            print(f"[airdrop] attempt {attempt}/3: balance={bal}, "
                  f"requesting {AIRDROP_LAMPORTS}...")
            sig = client.request_airdrop(pubkey, AIRDROP_LAMPORTS).value
            print(f"[airdrop] sig={sig}")
            for _ in range(30):
                time.sleep(1)
                bal = client.get_balance(pubkey).value
                if bal >= want:
                    return bal
        except Exception as e:  # noqa: BLE001
            last_err = e
            print(f"[airdrop] failed: {e!r}; retry in 5s")
            time.sleep(5)

    print(
        "\n[FAUCET BLOCKED] public devnet faucet did not respond after 3 tries\n"
        "  Manual fallback (one-shot, free):\n"
        f"    1. Open https://faucet.solana.com\n"
        f"    2. Paste address: {pubkey}\n"
        f"    3. Request 0.1 SOL on Devnet\n"
        f"    4. Re-run this script — it will skip airdrop and just attest.\n"
        f"  Last error: {last_err!r}"
    )
    raise SystemExit(3)


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv()
    kp_path = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if not kp_path:
        print("BLOCKED: SOLANA_KEYPAIR_PATH not set in env (.env).")
        return 2

    client = Client(DEVNET_RPC)
    attestor = Attestor(DEVNET_RPC, kp_path, cluster="devnet", client=client)
    pubkey = attestor.keypair.pubkey()
    print(f"[devnet] keypair={pubkey}")
    _ensure_balance(client, pubkey, want=5_000)  # ~0.000005 SOL fee floor

    # Use this script as the "artifact" so the sha is reproducible and
    # the test does not depend on the briefs/ dir existing.
    artifact = pathlib.Path(__file__).resolve()
    fake_trigger = "ATTEST_DEVNET_CHECK_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    print(f"[attest] trigger={fake_trigger} artifact={artifact.name}")
    result = attestor.attest(fake_trigger, [artifact])
    if result is None:
        print("[FAIL] attestor returned None — see warnings above")
        return 1

    print(f"[OK] tx_sig={result.tx_sig}")
    print(f"[OK] payload_sha256={result.payload_sha256}")
    print(f"[OK] explorer: {explorer_url(result.tx_sig, result.cluster)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
