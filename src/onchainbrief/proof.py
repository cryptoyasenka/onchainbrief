"""Build a public proof manifest (proof.json) for the feed.

One machine-readable file a judge (or anyone) can fetch to see the full audit
trail in one place: for each brief, the Solana trigger it summarizes, the
artifact hash, and the Solana Memo attestation that hash is bound to — plus the
SAP agent identity and the x402 payment rail. Everything here is derived from
the public briefs and their attestation sidecars; nothing is invented.
"""
from __future__ import annotations

import json
from pathlib import Path

from .feed import _parse_brief

# Known, on-chain-verifiable identity/payment anchors (see .planning history).
AGENT_PDA = "DsTZa5xY4sF8y3JFdE53B8T9xEsYtvntEUggm6FwMgVi"
CAPABILITY_ID = "onchainbrief:event-to-brief"
X402_NETWORK = "base"
USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
ACE_FACILITATOR = "0x4F0E2D3477a1B94CF33d16E442CEe4733dadCeE7"


def _solscan(sig: str, cluster: str) -> str:
    if not sig:
        return ""
    url = f"https://solscan.io/tx/{sig}"
    if cluster and cluster != "mainnet-beta":
        url += f"?cluster={cluster}"
    return url


def build_proof(briefs_dir: str | Path, out_path: str | Path) -> Path:
    """Write proof.json describing every brief in `briefs_dir`."""
    briefs_dir = Path(briefs_dir)
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    cards: list[dict] = []
    for md in sorted(briefs_dir.glob("*.md")):
        item = _parse_brief(md)
        if item is None:
            continue
        stem = md.stem
        attest: dict = {}
        sidecar = briefs_dir / f"{stem}.attest.json"
        if sidecar.exists():
            try:
                attest = json.loads(sidecar.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                attest = {}
        cluster = attest.get("cluster", item.attest_cluster) or ""
        attest_tx = attest.get("tx_sig", item.attest_sig) or ""
        cards.append({
            "id": stem,
            "category": item.category,
            "headline": item.headline,
            "trigger_signature": item.signature,
            "trigger_explorer": _solscan(item.signature, cluster),
            "card_image": item.card,
            "brief_markdown": item.md_name,
            "artifact_sha256": attest.get("artifact_sha256", ""),
            "memo_payload_sha256": attest.get("memo_payload_sha256", ""),
            "attestation_tx": attest_tx,
            "attestation_cluster": cluster,
            "attestation_explorer": _solscan(attest_tx, cluster),
            "sources": item.sources,
        })

    manifest = {
        "project": "OnchainBrief",
        "summary": (
            "Each brief binds an AI-written market summary to a real Solana "
            "trigger transaction, three paid Ace Data Cloud calls settled on "
            "Base via x402, and a Solana mainnet Memo receipt that the browser "
            "verifies byte-for-byte."
        ),
        "agent": {
            "protocol": "Synapse Agent Protocol (SAP)",
            "agent_pda": AGENT_PDA,
            "capability": CAPABILITY_ID,
        },
        "payments": {
            "protocol": "x402",
            "network": X402_NETWORK,
            "asset_usdc": USDC_BASE,
            "facilitator_pay_to": ACE_FACILITATOR,
        },
        "how_to_verify": (
            "For any card, recompute sha256(card_image bytes followed by "
            "brief_markdown bytes); it equals artifact_sha256, which is the "
            "'sha256' field inside the Memo payload of attestation_tx on the "
            "named Solana cluster."
        ),
        "card_count": len(cards),
        "cards": cards,
    }
    out_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return out_path
