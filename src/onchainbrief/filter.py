"""'Is this event worth a brief?' filter.

Runs on any LOCAL OpenAI-compatible endpoint (llama.cpp / vLLM / Ollama /
NVIDIA build, etc.) so it costs nothing and never spends ACE credits - only
events that pass here reach the paid pipeline. Falls back to a conservative
heuristic when no local model is configured, so the watcher path is
testable offline.
"""

from __future__ import annotations

import os

import requests

from .txfacts import SWAP_PROGRAMS as _SWAP_PROGRAMS
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


# EventFacts.kind -> feed category. Decoded facts beat log heuristics because
# they reflect what the transaction actually moved, not what it logged.
_KIND_TO_CATEGORY = {
    "deploy": "Deployment",
    "whale_transfer": "Volume",
    "token_transfer": "Volume",
    "swap": "Volume",
    "security": "Security",
    "governance": "Governance",
}


def categorize_event(ev: LogEvent) -> str:
    """Categorize an on-chain event, preferring decoded facts over log text."""
    facts = getattr(ev, "facts", None)
    if facts is not None:
        cat = _KIND_TO_CATEGORY.get(getattr(facts, "kind", ""))
        if cat:
            return cat

    logs_lower = "\n".join(ev.logs).lower()
    programs_lower = [p.lower() for p in ev.program_ids]

    # 1. Deployments and Upgrades
    if (
        "bpfloaderupgradeab1e11111111111111111111111" in programs_lower or
        any(k in logs_lower for k in ["upgrade", "deploy", "initialize", "init_program"])
    ):
        return "Deployment"

    # 2. Security Alerts / Admin Actions
    if any(k in logs_lower for k in ["set authority", "freeze", "disable", "pause", "paused", "multisig", "revoke", "exploit", "compromise", "attacker", "hack"]):
        return "Security"

    # 3. Market Movements / Large Volume
    defi_keywords = ["swap", "route", "liquidity", "heavy flow", "large transfer", "million", "pool", "mint", "burn"]
    # Real DEX/AMM program ids (lowercased to match programs_lower), single-sourced
    # from txfacts so this set can't drift into the mangled placeholders it once held.
    defi_programs = {p.lower() for p in _SWAP_PROGRAMS}
    if (
        any(p in defi_programs for p in programs_lower) or
        any(k in logs_lower for k in defi_keywords)
    ):
        return "Volume"

    # 4. Governance & Council voting
    if any(k in logs_lower for k in ["propose", "proposal", "vote", "voted", "council", "governance", "proposal_state"]):
        return "Governance"

    return "Activity"
