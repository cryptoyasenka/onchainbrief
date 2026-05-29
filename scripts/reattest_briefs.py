"""Rewrite the 4 launch briefs' body prose in plainer language, then RE-ATTEST
each on Solana mainnet so the on-chain memo's sha256 still matches the published
png+md (otherwise the feed's "Verify On-Chain" button would show a mismatch).

Only the body paragraph between the card image and `## Sources` is rewritten;
title / image / Sources / Provenance are preserved byte-for-byte. Per brief, if
the attest tx fails the .md is reverted to its original bytes so its EXISTING
sidecar + memo keep matching — the feed is never left in a broken state.

No ACE / x402 calls (no brief regeneration) — cost is one SPL-Memo fee per brief
from the attestor keypair. Idempotent: safe to re-run (re-attests, new tx sigs).

Run:  ATTEST_CLUSTER must be mainnet-beta in .env; .venv/Scripts/python.exe scripts/reattest_briefs.py
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

# (brief id, full trigger event sig, new body paragraph)
BRIEFS: list[tuple[str, str, str]] = [
    (
        "4PXfW42xg51VBNoa",
        "4PXfW42xg51VBNoaYuKYb6srjFQfXup4yAgYEEtRWVXJeck5cNML5JcqxNPd9bphNxPXvaiNqDZh19c2J25TnaGK",
        "A `FinalizeLockedStake` instruction was executed through the SPL Governance "
        "program in transaction `4PXfW42xg51VBNoaYuKYb6srjFQfXup4yAgYEEtRWVXJeck5cNML5"
        "JcqxNPd9bphNxPXvaiNqDZh19c2J25TnaGK`. SPL Governance is the standard on-chain "
        "framework DAOs on Solana use to manage proposals and voting, so an instruction "
        "at this level reflects a change to a DAO's governance state rather than ordinary "
        "token activity.",
    ),
    (
        "2YxfWHLxbs4hya1k",
        "2YxfWHLxbs4hya1kYaQSDRJdu528YfUFxgr3L7hiJWax7Drx2FMU1KpAi4c6ajjoufGtHXwHUALuYE3k2UmdsBfy",
        "A new program was deployed through the BPF upgradeable loader. Because it uses "
        "the upgradeable loader, the program's code can be changed later by whoever holds "
        "its upgrade authority — so that authority's identity and intentions are what "
        "determine how trustworthy the deployment is over time.",
    ),
    (
        "2mdJjk7WbnYpcDE2",
        "2mdJjk7WbnYpcDE2vj3GPTNdDY3SmBLs9o7s6mooDdT1cHsBUSqCQB3WBs4fWijragYqjruRzdYRLV5gQPndMyMa",
        "A new Solana program was deployed through the BPF upgradeable loader in "
        "transaction `2mdJjk7WbnYpcDE2vj3GPTNdDY3SmBLs9o7s6mooDdT1cHsBUSqCQB3WBs4fWijragYq"
        "jruRzdYRLV5gQPndMyMa`. A program on this loader can be modified while its upgrade "
        "authority remains active, so whether that authority is retained or revoked is the "
        "key question for its long-term immutability.",
    ),
    (
        "65PNPvjZgGRwPFne",
        "65PNPvjZgGRwPFneFnZZpjoLmqhRyw1qPPvPgBjuvzZMXmDTzmk3BKqFhZXGxe4KuFbf84ogBwLpiR5F3ejt1f9x",
        "The program `BvAPdAKHMsku` was upgraded through the BPF upgradeable loader. An "
        "upgrade replaces the program's on-chain code under its existing address, so "
        "anything that relied on the previous behavior should be re-checked against the "
        "new version.",
    ),
]


def rebuild_md(md_path: pathlib.Path, new_body: str) -> str:
    """Swap only the body paragraph; keep title/image/Sources/Provenance verbatim."""
    text = md_path.read_text(encoding="utf-8")
    idx = text.index("## Sources")
    head, tail = text[:idx], text[idx:]
    lines = head.split("\n")
    title = lines[0]
    assert title.startswith("# "), f"unexpected title line: {title!r}"
    img = next(l for l in lines if l.lstrip().startswith("!["))
    return f"{title}\n\n{img}\n\n{new_body}\n\n{tail}"


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
    for bid, trigger, body in BRIEFS:
        md = BR / f"{bid}.md"
        png = BR / f"{bid}.png"
        if not md.exists() or not png.exists():
            print(f"[SKIP] {bid}: md/png missing")
            failures.append(bid)
            continue
        original = md.read_text(encoding="utf-8")
        md.write_text(rebuild_md(md, body), encoding="utf-8")
        att = attestor.attest(trigger, [str(png), str(md)])
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
    print(f"\n[DONE] {ok}/{len(BRIEFS)} re-attested. failures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
