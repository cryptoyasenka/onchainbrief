"""x402 on-chain payment client for Ace Data Cloud.

Settles on Base mainnet in USDC using EIP-3009 TransferWithAuthorization.
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
    ACE_IMAGE_TASKS_PATH,
    X402_ALLOWED_PAY_TO,
    X402_EXPECTED_ASSET,
    X402_EXPECTED_CHAIN_ID,
    X402_EXPECTED_VERIFYING_CONTRACT,
    X402_MAX_AMOUNT_REQUIRED,
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


def _validate_accept(accept: dict) -> None:
    """Reject a 402 challenge before signing if it violates our spending policy.

    A signed EIP-3009 authorization is a bearer instrument: whoever holds it can
    pull `value` USDC from our wallet on the named chain. We therefore refuse to
    sign anything that exceeds the configured cap, targets the wrong chain/asset,
    or pays an address outside an (optional) allowlist — turning a compromised or
    buggy facilitator into a hard failure instead of a silent drain.
    """
    if accept.get("scheme") != "exact":
        raise X402Error(f"x402 policy: unexpected scheme {accept.get('scheme')!r}")
    if accept.get("network") != X402_NETWORK:
        raise X402Error(f"x402 policy: unexpected network {accept.get('network')!r}")

    try:
        amount = int(accept["maxAmountRequired"])
    except (KeyError, TypeError, ValueError):
        raise X402Error("x402 policy: missing/invalid maxAmountRequired")
    if amount <= 0:
        raise X402Error(f"x402 policy: non-positive amount {amount}")
    if amount > X402_MAX_AMOUNT_REQUIRED:
        raise X402Error(
            f"x402 policy: amount {amount} exceeds cap {X402_MAX_AMOUNT_REQUIRED} "
            "(atomic USDC) — raise X402_MAX_AMOUNT_REQUIRED if intended"
        )

    extra = accept.get("extra") or {}
    try:
        chain_id = int(extra["chainId"])
    except (KeyError, TypeError, ValueError):
        raise X402Error("x402 policy: missing/invalid extra.chainId")
    if chain_id != X402_EXPECTED_CHAIN_ID:
        raise X402Error(
            f"x402 policy: chainId {chain_id} != expected {X402_EXPECTED_CHAIN_ID}"
        )

    # Asset is not always present in the accept block; only enforce when given.
    asset = accept.get("asset")
    if asset and asset.lower() != X402_EXPECTED_ASSET.lower():
        raise X402Error(
            f"x402 policy: asset {asset} != expected {X402_EXPECTED_ASSET}"
        )

    if X402_EXPECTED_VERIFYING_CONTRACT:
        vc = str(extra.get("verifyingContract", ""))
        if vc.lower() != X402_EXPECTED_VERIFYING_CONTRACT.lower():
            raise X402Error(
                f"x402 policy: verifyingContract {vc} != expected "
                f"{X402_EXPECTED_VERIFYING_CONTRACT}"
            )

    allowed = [a.strip().lower() for a in X402_ALLOWED_PAY_TO.split(",") if a.strip()]
    if allowed:
        pay_to = str(accept.get("payTo", "")).lower()
        if pay_to not in allowed:
            raise X402Error(
                f"x402 policy: payTo {accept.get('payTo')!r} not in allowlist"
            )


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
        # ACE_API_TOKEN is NOT required - sending Bearer disables 402.
        self.settings.require("ace_x402_private_key")
        self.timeout = timeout
        self._account = Account.from_key(self.settings.ace_x402_private_key)
        self._s = requests.Session()
        self._s.headers.update({
            "accept": "application/json",
            "content-type": "application/json",
        })
        # Dynamic tool discovery via Solana SAP registry
        from .discovery import discover_ace_endpoints
        self.api_base, self.endpoints = discover_ace_endpoints(self.settings.solana_rpc_url)

    @property
    def pay_address(self) -> str:
        return self._account.address



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
                _validate_accept(accept)
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
        url = f"{self.api_base}{self.endpoints[service]}"
        timeout = max(self.timeout, 300.0) if service == "image" else self.timeout
        return self._post_x402(url, payload, timeout=timeout)

    def image_task(self, task_id: str) -> requests.Response:
        """Poll an async image task. Free per ACE docs - credit/x402 not charged."""
        return self._post_x402(
            f"{self.api_base}{ACE_IMAGE_TASKS_PATH}",
            {"id": task_id},
            timeout=self.timeout,
        )


