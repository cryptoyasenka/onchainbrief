#!/usr/bin/env python
"""Online proof rehearsal: prove the LIVE feed matches the chain, end to end.

Read-only and free: no on-chain writes, no spend, no publish. For every card
the live site serves, this re-derives the artifact hash from the served files
and confirms it equals the sha256 carried in that card's Solana Memo receipt,
so a judge can reproduce the whole trust story with one command.

Kept OUT of scripts/final_gate.py on purpose: that gate is offline and
deterministic, while this needs public HTTP + Solana RPC, which can be flaky.
Run it as a pre-submission online rehearsal, not as a build gate.

    python scripts/verify_live_proof.py
    python scripts/verify_live_proof.py --url https://<deploy> --rpc https://<rpc>
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from urllib.parse import urljoin

import requests

DEFAULT_URL = "https://onchainbrief-production.up.railway.app"
# Public mainnet RPC by default; pass --rpc for a private/paid endpoint if the
# public one rate-limits. Memos live on mainnet-beta (proof.json names it).
DEFAULT_RPC = "https://api.mainnet-beta.solana.com"
MEMO_PROGRAM_ID = "MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def _decode_base58(data: str) -> bytes:
    n = 0
    for ch in data:
        idx = _B58_ALPHABET.find(ch)
        if idx < 0:
            raise ValueError("invalid base58")
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(data) - len(data.lstrip("1"))) + raw


def _extract_memo_text(tx: dict) -> str:
    """The Memo payload string as it was stored on-chain (matches serve_feed)."""
    message = tx.get("transaction", {}).get("message", {})
    for ix in message.get("instructions", []) or []:
        if not isinstance(ix, dict) or ix.get("programId") != MEMO_PROGRAM_ID:
            continue
        parsed = ix.get("parsed")
        if isinstance(parsed, str):
            return parsed
        data = ix.get("data")
        if isinstance(data, str):
            try:
                return _decode_base58(data).decode("utf-8")
            except (ValueError, UnicodeDecodeError):
                return ""
    for log in tx.get("meta", {}).get("logMessages", []) or []:
        marker = "Program log: Memo (v2):"
        if marker in log:
            return log.split(marker, 1)[1].strip()
    return ""


def _get_bytes(url: str, timeout: int) -> bytes:
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content


def _get_tx(rpc: str, sig: str, timeout: int) -> dict | None:
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [sig, {"encoding": "jsonParsed",
                         "maxSupportedTransactionVersion": 0}],
    }
    r = requests.post(rpc, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json().get("result")


def verify(url: str, rpc: str, timeout: int) -> int:
    base = url.rstrip("/") + "/"
    proof = json.loads(_get_bytes(urljoin(base, "proof.json"), timeout))
    cards = proof.get("cards", [])
    declared = proof.get("card_count")
    print(f"Live proof: {url}  ({len(cards)} card(s), card_count={declared})")
    if declared != len(cards):
        print(f"  [FAIL] card_count {declared} != {len(cards)} cards listed")
        return 1

    failures = 0
    for c in cards:
        cid = c.get("id", "?")
        try:
            png = _get_bytes(urljoin(base, c["card_image"]), timeout)
            md = _get_bytes(urljoin(base, c["brief_markdown"]), timeout)
            served = hashlib.sha256(png + md).hexdigest()
            if served != c["artifact_sha256"]:
                print(f"  [FAIL] {cid}: served sha256 {served[:12]}.. != "
                      f"artifact_sha256 {c['artifact_sha256'][:12]}..")
                failures += 1
                continue

            tx = _get_tx(rpc, c["attestation_tx"], timeout)
            if not tx:
                print(f"  [FAIL] {cid}: attestation_tx not found on chain")
                failures += 1
                continue
            memo = _extract_memo_text(tx)
            if not memo:
                print(f"  [FAIL] {cid}: no Memo payload in attestation_tx")
                failures += 1
                continue
            if hashlib.sha256(memo.encode("utf-8")).hexdigest() != c["memo_payload_sha256"]:
                print(f"  [FAIL] {cid}: live Memo payload sha256 mismatch")
                failures += 1
                continue
            body = {}
            try:
                body = json.loads(memo)
            except ValueError:
                pass
            if body.get("sha256") and body["sha256"] != c["artifact_sha256"]:
                print(f"  [FAIL] {cid}: Memo sha256 != served artifact hash")
                failures += 1
                continue
            if body.get("trigger") and body["trigger"] != c["trigger_signature"]:
                print(f"  [FAIL] {cid}: Memo trigger != card trigger_signature")
                failures += 1
                continue
            print(f"  [MATCH] {cid}: served == artifact == on-chain Memo "
                  f"({c.get('category', '?')})")
        except requests.RequestException as e:
            print(f"  [FAIL] {cid}: network error {e!r}")
            failures += 1

    print("=" * 60)
    if failures:
        print(f"RESULT: FAIL ({failures}/{len(cards)} card(s) failed)")
        return 1
    print(f"RESULT: PASS (all {len(cards)} live cards hash to their on-chain Memo)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Online proof rehearsal (read-only).")
    ap.add_argument("--url", default=DEFAULT_URL, help="live feed base URL")
    ap.add_argument("--rpc", default=DEFAULT_RPC, help="Solana mainnet RPC URL")
    ap.add_argument("--timeout", type=int, default=20, help="per-request seconds")
    args = ap.parse_args(argv)
    try:
        return verify(args.url, args.rpc, args.timeout)
    except requests.RequestException as e:
        print(f"RESULT: ERROR - could not reach live feed: {e!r}")
        return 2
    except (ValueError, KeyError) as e:
        print(f"RESULT: ERROR - malformed proof.json: {e!r}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
