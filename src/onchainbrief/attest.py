"""Per-brief on-chain attestation - Memo-program tx that binds the artifact
hash to its trigger event.

The wash-resistance proof: every published brief leaves an on-chain receipt
whose payload is `{v, app, cap, trigger, sha256, ts}` - anyone can fetch the
artifacts, hash them, and verify the memo. Trigger sig binds the brief to a
real on-chain event; the artifact sha binds the receipt to its content.

Resilience contract (same as pipeline.handle): attest failure must NOT block
publishing - it returns `None` and the brief ships without a second anchor.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solders.instruction import Instruction
from solders.keypair import Keypair
from solders.message import Message
from solders.pubkey import Pubkey
from solders.transaction import Transaction

# SPL Memo v2 - the canonical memo program on every Solana cluster.
MEMO_PROGRAM_ID = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")

# Single source of truth for the payload's static fields; mirrors the SAP
# capability id. Cap_id uses the on-chain SAP `<protocol>:<kebab-name>` convention.
APP_ID = "onchainbrief"
CAP_ID = "onchainbrief:event-to-brief"
PAYLOAD_VERSION = 1

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Attestation:
    tx_sig: str
    cluster: str
    # sha256 of the canonical memo *payload* bytes (the {v,app,cap,trigger,
    # sha256,ts} JSON that was sent to the Memo program).
    memo_payload_sha256: str
    # sha256 of the *artifacts* (card png + brief md, concatenated in order).
    # This is the value carried in the memo payload's own "sha256" field and is
    # what a verifier reproduces from the published files. Distinct from the
    # payload hash above so the sidecar names both without ambiguity.
    artifact_sha256: str = ""


def _artifact_sha256(paths: Iterable[str | Path]) -> str:
    """Concat-hash artifact bytes in the given order - deterministic."""
    h = hashlib.sha256()
    for p in paths:
        h.update(Path(p).read_bytes())
    return h.hexdigest()


def _payload_for(trigger_sig: str, artifact_sha: str, ts: str | None = None) -> bytes:
    """Canonical JSON memo payload (sorted keys, no whitespace, UTF-8).

    Sorted-keys + no-whitespace = byte-stable for sha matching by any verifier
    that reconstructs the payload from the same inputs.
    """
    payload = {
        "v": PAYLOAD_VERSION,
        "app": APP_ID,
        "cap": CAP_ID,
        "trigger": trigger_sig,
        "sha256": artifact_sha,
        "ts": ts or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _build_memo_ix(payload: bytes) -> Instruction:
    """One Memo-program instruction with payload as data.

    SPL Memo accepts zero accounts when no signers are required as memo
    witnesses; the fee-payer's signature on the tx is enough to bind the
    memo to that key.
    """
    return Instruction(program_id=MEMO_PROGRAM_ID, accounts=[], data=payload)


class Attestor:
    """Sends one Memo-program tx per brief.

    The RPC client is injected so tests can mock send/confirm without network.
    """

    def __init__(
        self,
        rpc_url: str,
        keypair_path: str | Path,
        *,
        cluster: str = "devnet",
        client: Any | None = None,
    ) -> None:
        self.rpc_url = rpc_url
        self.cluster = cluster
        self.keypair = Keypair.from_json(Path(keypair_path).read_text(encoding="utf-8"))
        if client is None:
            from solana.rpc.api import Client

            client = Client(rpc_url)
        self._client = client

    def attest(
        self,
        trigger_sig: str,
        artifact_paths: list[str | Path],
        *,
        ts: str | None = None,
    ) -> Attestation | None:
        """Build → sign → send a memo tx. Returns None on any failure."""
        try:
            artifact_sha = _artifact_sha256(artifact_paths)
            payload = _payload_for(trigger_sig, artifact_sha, ts=ts)
            ix = _build_memo_ix(payload)
            blockhash = self._client.get_latest_blockhash().value.blockhash
            msg = Message.new_with_blockhash([ix], self.keypair.pubkey(), blockhash)
            tx = Transaction([self.keypair], msg, blockhash)
            resp = self._client.send_transaction(tx)
            sig = str(resp.value)
            # Best-effort confirm; do not let a confirm-timeout swallow a sig
            # that actually landed.
            try:
                self._client.confirm_transaction(resp.value)
            except Exception as e:  # noqa: BLE001
                _log.warning("attest confirm timed out for %s: %r", sig, e)
            payload_sha = hashlib.sha256(payload).hexdigest()
            return Attestation(
                tx_sig=sig,
                cluster=self.cluster,
                memo_payload_sha256=payload_sha,
                artifact_sha256=artifact_sha,
            )
        except Exception as e:  # noqa: BLE001 - resilience boundary
            _log.warning("attest failed for trigger %s: %r", trigger_sig, e)
            return None


def explorer_url(sig: str, cluster: str) -> str:
    """Human-readable explorer link; devnet needs the cluster suffix."""
    base = f"https://solscan.io/tx/{sig}"
    return base if cluster == "mainnet-beta" else f"{base}?cluster={cluster}"
