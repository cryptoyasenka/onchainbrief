"""Empirical cost step: one real call to each of the 3 ACE services, record
actual credit/USD burn. Run with a funded ACE_API_TOKEN. Costs are measured,
never assumed.

Usage:  python scripts/measure_credits.py
Output: prints per-service status + usage; writes a measurements note under
        the gitignored .planning/ dir (kept local — see .gitignore).
"""

from __future__ import annotations

import datetime
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from onchainbrief.ace_client import AceClient, AceError  # noqa: E402
from onchainbrief.config import Settings  # noqa: E402

OUT = pathlib.Path(__file__).resolve().parents[1] / ".planning" / "ACE-MEASUREMENTS.md"

PROBES = [
    ("chat", lambda c: c.chat("Reply with the single word: ok")),
    ("serp", lambda c: c.serp("solana mainnet status")),
    ("image", lambda c: c.image("a minimal flat-design trading card, blue")),
]


def main() -> int:
    s = Settings.load()
    if not s.ace_api_token:
        print("BLOCKED: ACE_API_TOKEN not set. Get the per-service token from "
              "platform.acedata.cloud dashboard, put it in .env, re-run.")
        return 2

    client = AceClient(s)
    rows: list[str] = []
    for name, fn in PROBES:
        try:
            resp = fn(client)
            body = resp.text[:300]
            try:
                usage = resp.json().get("usage")
            except Exception:
                usage = None
            line = f"- **{name}**: HTTP {resp.status_code} | usage={usage} | body[:300]={body!r}"
            print(line)
            rows.append(line)
        except AceError as e:
            line = f"- **{name}**: ERROR {e}"
            print(line)
            rows.append(line)

    stamp = datetime.datetime.now(
        datetime.timezone.utc
    ).strftime("%Y-%m-%d %H:%M UTC")
    OUT.write_text(
        f"# Empirical ACE per-service measurement\n\n"
        f"Measured: {stamp}\n\n"
        f"{chr(10).join(rows)}\n\n"
        f"Next: check platform.acedata.cloud credit balance before/after to "
        f"derive real credits/call, then recompute per-event budget + throttle.\n",
        encoding="utf-8",
    )
    print(f"\nWrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
