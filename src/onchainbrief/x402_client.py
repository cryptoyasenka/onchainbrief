"""x402 on-chain payment client for Ace Data Cloud.

Settles on Base mainnet in USDC; ACE's facilitator covers gas (wallet only
needs a few USDC). Empirical finding (2026-05-21, see .planning/X402-EVIDENCE):

  * `authorization: Bearer ...` on the service endpoint → credit-billed (the
    response carries `X-Usage-Exempt: true`, no on-chain settlement)
  * NO `authorization` header → HTTP 402 with the full x402 challenge body
    (`accepts[]` per network). Sign EIP-3009 TransferWithAuthorization for
    the base/exact entry, retry with `X-PAYMENT: <base64(JSON)>` → HTTP 200
    with the service result, and the facilitator's `transferWithAuthorization`
    tx lands on Base shortly after.

We sign manually with `eth_account` because the x402 Python libs (Coinbase
0.3.x, foundation 2.x) both fail Pydantic validation on ACE's challenge
(multi-network `accepts[]` includes networks the libs' enums don't allow).
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Any

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data

from .config import (
    ACE_API_BASE,
    ACE_ENDPOINTS,
    ACE_IMAGE_TASKS_PATH,
    ACE_ORDER_PAY_PATH,
    ACE_PLATFORM_BASE,
    X402_NETWORK,
    Settings,
)


class X402Error(RuntimeError):
    pass


def _pick_accept(challenge: dict) -> dict:
    """Pick the base/exact entry from a 402 challenge `accepts` array."""
    for a in challenge.get("accepts", []):
        if a.get("network") == X402_NETWORK and a.get("scheme") == "exact":
            return a
    raise X402Error(f"no base/exact entry in challenge: {challenge}")


def _sign_x_payment(account, accept: dict) -> str:
    """Sign EIP-3009 TransferWithAuthorization, return base64(X-PAYMENT JSON)."""
    extra = accept["extra"]
    now = int(time.time())
    valid_after = 0
    valid_before = now + int(accept.get("maxTimeoutSeconds", 600))
    nonce_hex = "0x" + secrets.token_bytes(32).hex()

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
    signed = account.sign_message(encode_typed_data(full_message=typed))
    payload = {
        "x402Version": 2,
        "scheme": "exact",
        "network": X402_NETWORK,
        "payload": {
            "signature": "0x" + signed.signature.hex().removeprefix("0x"),
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
    return base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()


class X402Client:
    def __init__(self, settings: Settings | None = None, timeout: float = 120.0):
        self.settings = settings or Settings.load()
        # ACE_API_TOKEN is NOT required — sending Bearer disables 402.
        self.settings.require("ace_x402_private_key")
        self.timeout = timeout
        self._account = Account.from_key(self.settings.ace_x402_private_key)
        self._s = requests.Session()
        self._s.headers.update({
            "accept": "application/json",
            "content-type": "application/json",
        })

    @property
    def pay_address(self) -> str:
        return self._account.address

    @staticmethod
    def decode_payment_response(resp) -> dict | None:
        """X-PAYMENT-RESPONSE is OPTIONAL per spec; ACE doesn't return it.
        Canonical proof is the on-chain USDC Transfer log (see X402-EVIDENCE).
        """
        raw = resp.headers.get("X-PAYMENT-RESPONSE")
        if not raw:
            return None
        try:
            return json.loads(base64.b64decode(raw))
        except Exception:
            return {"raw": raw[:200]}

    def _post_x402(self, url: str, payload: dict[str, Any], *, timeout: float) -> requests.Response:
        for attempt in range(1, 3):
            try:
                r1 = self._s.post(url, json=payload, timeout=timeout)
                if r1.status_code != 402:
                    if r1.status_code >= 500 and attempt < 2:
                        time.sleep(2.0 * attempt)
                        continue
                    if r1.status_code >= 400:
                        raise X402Error(f"{url} initial {r1.status_code}: {r1.text[:500]}")
                    return r1
                try:
                    challenge = r1.json()
                except Exception:
                    raise X402Error(f"{url} 402 body not JSON: {r1.text[:500]}")
                accept = _pick_accept(challenge)
                x_payment = _sign_x_payment(self._account, accept)
                r2 = self._s.post(
                    url, json=payload, timeout=timeout,
                    headers={"X-PAYMENT": x_payment},
                )
                if r2.status_code >= 500 and attempt < 2:
                    time.sleep(2.0 * attempt)
                    continue
                if r2.status_code >= 400:
                    raise X402Error(f"{url} retry {r2.status_code}: {r2.text[:500]}")
                return r2
            except requests.RequestException as e:
                if attempt < 2:
                    time.sleep(2.0 * attempt)
                    continue
                raise X402Error(f"{url} network error: {e!r}")


    def call(self, service: str, payload: dict[str, Any]) -> requests.Response:
        url = f"{ACE_API_BASE}{ACE_ENDPOINTS[service]}"
        timeout = max(self.timeout, 300.0) if service == "image" else self.timeout
        return self._post_x402(url, payload, timeout=timeout)

    def image_task(self, task_id: str) -> requests.Response:
        """Poll an async image task. Free per ACE docs — credit/x402 not charged."""
        return self._post_x402(
            f"{ACE_API_BASE}{ACE_IMAGE_TASKS_PATH}",
            {"id": task_id},
            timeout=self.timeout,
        )

    # --- Flow B (explicit order pay) — kept for pre-created console orders ---

    def pay_order(self, order_id: str) -> requests.Response:
        url = f"{ACE_PLATFORM_BASE}{ACE_ORDER_PAY_PATH.format(order_id=order_id)}"
        return self._post_x402(url, {"pay_way": "X402"}, timeout=self.timeout)
