"""Daily spend gate.

Low-spend lane: at most N briefs/day. N is intentionally conservative until
the real per-service ACE credit burn is measured on a funded run (measured,
never assumed). Persisted to a gitignored state file so a restart can't
blow the daily budget.
"""

from __future__ import annotations

import datetime
import json
import os
import pathlib

STATE_PATH = pathlib.Path(os.getenv("THROTTLE_STATE", ".state/throttle.json"))
# Conservative default; bump only once a funded run measures real credits/event.
DAILY_CAP = int(os.getenv("DAILY_BRIEF_CAP", "1"))


def _today() -> str:
    # timezone.utc (not datetime.UTC) — UTC alias is 3.11+, but this runs on
    # every brief and pyproject floors at 3.10 (Railway nixpacks isn't pinned).
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")


def _load() -> dict:
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {}


def remaining_today() -> int:
    st = _load()
    used = st.get(_today(), 0)
    return max(0, DAILY_CAP - used)


def try_consume() -> bool:
    """Reserve one brief for today. False if the daily cap is reached."""
    st = _load()
    day = _today()
    used = st.get(day, 0)
    if used >= DAILY_CAP:
        return False
    st[day] = used + 1
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(st))
    return True
