"""Manual x402 (EIP-3009 TransferWithAuthorization) round-trip probe.

Bypasses the x402 0.3.x lib (Pydantic schema rejects ACE's multi-network
accepts array). Signs TransferWithAuthorization directly with eth_account
and posts X-PAYMENT base64(JSON) header.

For each ACE service:
  1. POST service endpoint with NO Authorization header
  2. Receive HTTP 402 + accepts[]
  3. Pick the first 'base' network entry (USDC on Base mainnet)
  4. Sign EIP-3009 TransferWithAuthorization
  5. Retry with X-PAYMENT header
  6. Capture HTTP 200 + X-PAYMENT-RESPONSE (tx hash via Ace facilitator)
  7. Write evidence to .planning/X402-EVIDENCE.md (basescan tx links)
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import pathlib
import secrets
import sys
import time

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from onchainbrief.config import ACE_API_BASE, ACE_ENDPOINTS, Settings  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / ".planning" / "X402-EVIDENCE.md"

PROBES: list[tuple[str, dict]] = [
    ("chat", {"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": "Reply with the single word: ok"}]}),
    ("serp", {"query": "solana mainnet status", "number": 5}),
    ("image", {"action": "generate", "model": "nano-banana",
               "prompt": "a minimal flat-design trading card, blue",
               "size": "16:9", "count": 1}),
]


def pick_base_accept(challenge: dict) -> dict:
    for a in challenge.get("accepts", []):
        if a.get("network") == "base" and a.get("scheme") == "exact":
            return a
    raise RuntimeError(f"No base/exact entry in challenge: {challenge}")


def sign_x_payment(account, accept: dict) -> str:
    extra = accept["extra"]
    now = int(time.time())
    valid_after = 0  # accept any past timestamp; 0 = unbounded past
    valid_before = now + int(accept.get("maxTimeoutSeconds", 600))
    nonce_bytes = secrets.token_bytes(32)
    nonce_hex = "0x" + nonce_bytes.hex()

    typed = {
        "domain": {
            "name": extra["name"],
            "version": extra["version"],
            "chainId": int(extra["chainId"]),
            "verifyingContract": extra["verifyingContract"],
        },
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "TransferWithAuthorization": [
                {"name": "from", "type": "address"},
                {"name": "to", "type": "address"},
                {"name": "value", "type": "uint256"},
                {"name": "validAfter", "type": "uint256"},
                {"name": "validBefore", "type": "uint256"},
                {"name": "nonce", "type": "bytes32"},
            ],
        },
        "primaryType": "TransferWithAuthorization",
        "message": {
            "from": account.address,
            "to": accept["payTo"],
            "value": int(accept["maxAmountRequired"]),
            "validAfter": valid_after,
            "validBefore": valid_before,
            "nonce": nonce_hex,
        },
    }
    signable = encode_typed_data(full_message=typed)
    signed = account.sign_message(signable)
    signature = "0x" + signed.signature.hex().lstrip("0x")

    x_payment = {
        "x402Version": 2,
        "scheme": "exact",
        "network": "base",
        "payload": {
            "signature": signature,
            "authorization": {
                "from": account.address,
                "to": accept["payTo"],
                "value": str(int(accept["maxAmountRequired"])),
                "validAfter": str(valid_after),
                "validBefore": str(valid_before),
                "nonce": nonce_hex,
            },
        },
    }
    raw = json.dumps(x_payment, separators=(",", ":")).encode()
    return base64.b64encode(raw).decode()


def decode_payment_response(header_val: str) -> dict | None:
    if not header_val:
        return None
    try:
        return json.loads(base64.b64decode(header_val))
    except Exception:
        return {"raw": header_val[:200]}


def probe_service(account, service: str, payload: dict) -> dict:
    url = f"{ACE_API_BASE}{ACE_ENDPOINTS[service]}"
    timeout = 300 if service == "image" else 120

    r1 = requests.post(url, json=payload, timeout=timeout)
    if r1.status_code != 402:
        return {
            "service": service, "ok": False,
            "stage": "initial-not-402", "status": r1.status_code,
            "body": r1.text[:400],
        }
    challenge = r1.json()
    try:
        accept = pick_base_accept(challenge)
    except Exception as e:
        return {"service": service, "ok": False, "stage": "no-base-accept", "error": str(e)}

    try:
        x_payment_b64 = sign_x_payment(account, accept)
    except Exception as e:
        return {"service": service, "ok": False, "stage": "sign-failed",
                "error": f"{type(e).__name__}: {e}"}

    headers = {"X-PAYMENT": x_payment_b64, "Content-Type": "application/json"}
    r2 = requests.post(url, json=payload, headers=headers, timeout=timeout)
    settlement = decode_payment_response(r2.headers.get("X-PAYMENT-RESPONSE", ""))

    return {
        "service": service,
        "ok": r2.status_code == 200 and settlement is not None,
        "initial_status": r1.status_code,
        "retry_status": r2.status_code,
        "amount_usdc_atomic": int(accept["maxAmountRequired"]),
        "amount_usdc": int(accept["maxAmountRequired"]) / 1e6,
        "pay_to": accept["payTo"],
        "settlement": settlement,
        "body_preview": r2.text[:240],
    }


def main() -> int:
    s = Settings.load()
    if not s.ace_x402_private_key:
        print("BLOCKED: ACE_X402_PRIVATE_KEY not set in .env")
        return 2
    account = Account.from_key(s.ace_x402_private_key)
    print(f"x402 pay address: {account.address}")

    rows: list[str] = []
    results = []
    for name, payload in PROBES:
        print(f"\n--- {name} ---")
        try:
            r = probe_service(account, name, payload)
            results.append(r)
            print(json.dumps(r, indent=2, default=str)[:1500])
        except Exception as e:
            r = {"service": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
            results.append(r)
            print(r)

    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [f"# x402 round-trip evidence (manual EIP-3009 signing)\n",
             f"Measured: {stamp}\n",
             f"Pay address: `{account.address}`\n",
             f"\nFlow: POST service endpoint without Authorization → HTTP 402 with accepts[] → "
             f"sign EIP-3009 TransferWithAuthorization for USDC on Base → retry with X-PAYMENT base64 "
             f"header → HTTP 200 + X-PAYMENT-RESPONSE carries the on-chain settlement tx hash.\n\n"]
    for r in results:
        s = r["service"]
        if r.get("ok"):
            tx = r["settlement"].get("transaction") or r["settlement"].get("tx_hash") or "?"
            lines.append(
                f"### {s}\n"
                f"- 402→200 OK\n"
                f"- Amount: **{r['amount_usdc']:.6f} USDC** (`{r['amount_usdc_atomic']}` atomic) → `{r['pay_to']}`\n"
                f"- Tx hash: `{tx}` — https://basescan.org/tx/{tx}\n"
                f"- Settlement: `{json.dumps(r['settlement'], default=str)[:400]}`\n"
                f"- Body: `{r['body_preview']!r}`\n\n"
            )
        else:
            lines.append(f"### {s}\n- FAILED — `{json.dumps(r, default=str)[:600]}`\n\n")

    OUT.write_text("".join(lines), encoding="utf-8")
    print(f"\nWrote {OUT}")
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
