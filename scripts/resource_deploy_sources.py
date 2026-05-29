"""Re-source the 3 deploy/upgrade briefs with on-chain evidence, then RE-ATTEST
on Solana mainnet so each brief's on-chain memo still matches its png+md.

WHY: all 3 deploy cards shared the same 5 generic tutorial links (the SERP query
for a deploy is a fixed topic string — pipeline.py:94), which reads as filler.
A market brief's source should be evidence for the claim:
  - 65PNPvjZ (upgrade of a resolvable program): cite the program account + its
    upgrade authority (verified on-chain via scripts/preview_onchain_sources.py)
    + one canonical doc.
  - 2mdJjk7W / 2YxfWHLx (anonymous fresh deploys, no program-id in loader logs):
    there is nothing program-specific to cite, so the source is the transaction
    itself, and the body says so plainly.

Only the body (2 fresh deploys) and the ## Sources block change; title / image /
Provenance stay byte-identical. Per brief, a failed attest reverts the .md so its
existing sidecar+memo keep matching — the feed is never left half-broken.

No ACE / x402 (no regeneration) — cost is one SPL-Memo fee per brief. Idempotent.
Run:  ATTEST_CLUSTER=mainnet-beta in .env; .venv/Scripts/python.exe scripts/resource_deploy_sources.py
"""

from __future__ import annotations

import json
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from onchainbrief.attest import Attestor  # noqa: E402

BR = ROOT / "briefs"

_ANON = (
    "The program has no public identity yet — no name, site, or documentation — "
    "so the transaction itself is the only record available to reason from. "
)

BRIEFS: list[dict] = [
    {
        "id": "65PNPvjZgGRwPFne",
        "trigger": "65PNPvjZgGRwPFneFnZZpjoLmqhRyw1qPPvPgBjuvzZMXmDTzmk3BKqFhZXGxe4KuFbf84ogBwLpiR5F3ejt1f9x",
        # body unchanged
        "body_edits": [],
        "sources": [
            "https://solscan.io/account/BvAPdAKHMskuT3sVxkgrvsboNHRnbf2rXexrMh2E3RKi",
            "https://solscan.io/account/94JNQxXp6q95kW6p5uobgRzMdLqQztpy89CPFS89EZWC",
            "https://solana.com/docs/core/programs/program-deployment",
        ],
    },
    {
        "id": "2mdJjk7WbnYpcDE2",
        "trigger": "2mdJjk7WbnYpcDE2vj3GPTNdDY3SmBLs9o7s6mooDdT1cHsBUSqCQB3WBs4fWijragYqjruRzdYRLV5gQPndMyMa",
        "body_edits": [
            ("PndMyMa`. A program on this loader", f"PndMyMa`. {_ANON}A program on this loader"),
        ],
        "sources": [
            "https://solscan.io/tx/2mdJjk7WbnYpcDE2vj3GPTNdDY3SmBLs9o7s6mooDdT1cHsBUSqCQB3WBs4fWijragYqjruRzdYRLV5gQPndMyMa",
        ],
    },
    {
        "id": "2YxfWHLxbs4hya1k",
        "trigger": "2YxfWHLxbs4hya1kYaQSDRJdu528YfUFxgr3L7hiJWax7Drx2FMU1KpAi4c6ajjoufGtHXwHUALuYE3k2UmdsBfy",
        "body_edits": [
            ("BPF upgradeable loader. Because it uses", f"BPF upgradeable loader. {_ANON}Because it uses"),
        ],
        "sources": [
            "https://solscan.io/tx/2YxfWHLxbs4hya1kYaQSDRJdu528YfUFxgr3L7hiJWax7Drx2FMU1KpAi4c6ajjoufGtHXwHUALuYE3k2UmdsBfy",
        ],
    },
]


def rebuild_md(md_path: pathlib.Path, body_edits: list[tuple[str, str]], sources: list[str]) -> str:
    """Apply body insertions, then swap the ## Sources block. Title/image/Provenance verbatim."""
    text = md_path.read_text(encoding="utf-8")
    for old, new in body_edits:
        assert old in text, f"{md_path.name}: body anchor not found: {old!r}"
        text = text.replace(old, new, 1)
    assert "## Sources" in text and "## Provenance" in text, f"{md_path.name}: missing sections"
    pre, after = text.split("## Sources", 1)
    _src, provenance = after.split("## Provenance", 1)
    src_block = "\n".join(f"- {u}" for u in sources)
    return f"{pre}## Sources\n{src_block}\n\n## Provenance{provenance}"


def main() -> int:
    kp = os.getenv("SOLANA_KEYPAIR_PATH", "")
    rpc = os.getenv("ATTEST_RPC_URL")
    if not kp or not os.path.exists(os.path.expandvars(os.path.expanduser(kp))):
        print("BLOCKED: SOLANA_KEYPAIR_PATH missing")
        return 2
    if not rpc:
        print("BLOCKED: ATTEST_RPC_URL not set")
        return 2
    if os.getenv("ATTEST_CLUSTER") != "mainnet-beta":
        print(f"BLOCKED: ATTEST_CLUSTER={os.getenv('ATTEST_CLUSTER')!r}, want mainnet-beta")
        return 2

    attestor = Attestor(rpc, kp, cluster="mainnet-beta")
    failures: list[str] = []
    for b in BRIEFS:
        bid = b["id"]
        md = BR / f"{bid}.md"
        png = BR / f"{bid}.png"
        if not md.exists() or not png.exists():
            print(f"[SKIP] {bid}: md/png missing")
            failures.append(bid)
            continue
        original = md.read_text(encoding="utf-8")
        try:
            md.write_text(rebuild_md(md, b["body_edits"], b["sources"]), encoding="utf-8")
        except AssertionError as e:
            print(f"[FAIL] {bid}: {e}")
            failures.append(bid)
            continue
        att = attestor.attest(b["trigger"], [str(png), str(md)])
        if att is None:
            md.write_text(original, encoding="utf-8")  # revert: keep old sidecar/memo valid
            print(f"[FAIL] {bid}: attest returned None — .md reverted, sidecar untouched")
            failures.append(bid)
            continue
        (BR / f"{bid}.attest.json").write_text(
            json.dumps(
                {
                    "tx_sig": att.tx_sig,
                    "cluster": att.cluster,
                    "memo_payload_sha256": att.memo_payload_sha256,
                    "artifact_sha256": att.artifact_sha256,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"[OK]   {bid}: tx={att.tx_sig} artifact_sha256={att.artifact_sha256}")

    ok = len(BRIEFS) - len(failures)
    print(f"\n[DONE] {ok}/{len(BRIEFS)} re-sourced + re-attested. failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
