"""'Is this event worth a brief?' filter.

Runs on any LOCAL OpenAI-compatible endpoint (llama.cpp / vLLM / Ollama /
NVIDIA build, etc.) so it costs nothing and never spends ACE credits — only
events that pass here reach the paid pipeline. Falls back to a conservative
heuristic when no local model is configured, so the watcher path is
testable offline.
"""

from __future__ import annotations

import os

import requests

from .watcher import LogEvent

LOCAL_LLM_BASE = os.getenv("LOCAL_LLM_BASE", "")  # e.g. http://localhost:8080/v1
LOCAL_LLM_MODEL = os.getenv("LOCAL_LLM_MODEL", "local-model")
LOCAL_LLM_KEY = os.getenv("LOCAL_LLM_KEY", "")

_SYSTEM = (
    "You triage Solana mainnet events. Answer with exactly YES or NO: is this "
    "event notable enough for a short public market brief (a meaningful new "
    "program, sizable flow, launch, or governance move)? Routine noise = NO."
)


def _heuristic(ev: LogEvent) -> bool:
    # No model available: be conservative. Multi-program CPI with a sizable
    # log trail is a weak proxy for 'something non-trivial happened'.
    return len(ev.program_ids) >= 2 and len(ev.logs) >= 8


def is_brief_worthy(ev: LogEvent, timeout: float = 20.0) -> bool:
    if not LOCAL_LLM_BASE:
        return _heuristic(ev)
    try:
        payload = {
            "model": LOCAL_LLM_MODEL,
            "messages": [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"sig={ev.signature}\nprograms={ev.program_ids}\n"
                        f"logs:\n" + "\n".join(ev.logs[:30])
                    ),
                },
            ],
            "temperature": 0,
            "max_tokens": 3,
        }
        headers = {"content-type": "application/json"}
        if LOCAL_LLM_KEY:
            headers["authorization"] = f"Bearer {LOCAL_LLM_KEY}"
        r = requests.post(
            f"{LOCAL_LLM_BASE.rstrip('/')}/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
        r.raise_for_status()
        ans = r.json()["choices"][0]["message"]["content"].strip().upper()
        return ans.startswith("YES")
    except Exception:
        # Never let a triage outage spend credits; default to skip.
        return False
