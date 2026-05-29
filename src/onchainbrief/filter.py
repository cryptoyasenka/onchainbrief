"""'Is this event worth a brief?' filter.

Runs on any LOCAL OpenAI-compatible endpoint (llama.cpp / vLLM / Ollama /
NVIDIA build, etc.) so it costs nothing and never spends ACE credits - only
events that pass here reach the paid pipeline. Falls back to a conservative
heuristic when no local model is configured, so the watcher path is
testable offline.
"""

from __future__ import annotations

import json
import os
import pathlib

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


# --- Significance gate (runs on ENRICHED facts, after enrich_event) ---------
# is_brief_worthy above is a cheap, fact-blind first pass (LLM/heuristic) that
# avoids decoding obvious noise. This is the hard editorial bar: it sees the
# decoded amount/kind, so it can say "a $0.11 swap is not worth a brief" - the
# decision the old pre-enrich gate could never make.
#
# Every knob here is operator-configurable so whoever self-hosts the agent can
# point it at THEIR wallet (env keys, see config.py) and tune WHAT it briefs
# without touching code: the dollar bar plus on/off switches for the three
# value-independent event types. Resolution order, low to high precedence:
#   built-in defaults  <  environment (.env)  <  .state/watch_config.json
# The JSON layer is what the local operator panel writes, so a setting change
# takes effect on the next event without a redeploy.

# Defaults a fresh clone runs with. $10k swap bar; all event types watched.
# $10k is the floor for a swap/transfer to be "meaningful volume" - high enough
# to drop sub-$1 dust and small retail trades, low enough that the Volume tab is
# actually populatable (a $100k bar surfaced zero reachable swaps). Operators
# raise or lower it per their wallet via MIN_BRIEF_USD / the operator panel.
_DEFAULT_MIN_BRIEF_USD = 10_000.0
_VALUE_MOVE_KINDS = {"swap", "token_transfer", "whale_transfer"}
# Event kinds notable by their NATURE, not their dollar size: a freeze/
# setAuthority, a governance proposal action, or a real protocol deploy/upgrade
# is news even at $0 of value moved. Each maps to the env/JSON switch that
# turns its watching on or off; pure value-moves always need to clear the bar.
_NATURE_KINDS = {
    "security": "security",
    "governance": "governance",
    "deploy": "deploys",
}

# Where the operator panel persists live overrides (see serve_feed.py). Env so a
# read-only deploy can relocate it onto a writable volume.
WATCH_CONFIG_PATH = os.getenv("WATCH_CONFIG_PATH", ".state/watch_config.json")


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def watch_config() -> dict:
    """The agent's current watch parameters, freshly resolved on every call.

    Read live (not cached at import) so the operator panel writing
    `.state/watch_config.json` retunes the running agent without a restart.
    Keys: min_usd (float), deploys/governance/security (bool).
    """
    cfg = {
        "min_usd": float(os.getenv("MIN_BRIEF_USD", str(_DEFAULT_MIN_BRIEF_USD))),
        "deploys": _env_bool("WATCH_DEPLOYS", True),
        "governance": _env_bool("WATCH_GOVERNANCE", True),
        "security": _env_bool("WATCH_SECURITY", True),
    }
    try:
        override = json.loads(
            pathlib.Path(WATCH_CONFIG_PATH).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return cfg  # no file / unreadable / bad JSON -> env defaults stand
    if isinstance(override, dict):
        if "min_usd" in override:
            try:
                cfg["min_usd"] = float(override["min_usd"])
            except (TypeError, ValueError):
                pass
        for key in ("deploys", "governance", "security"):
            if key in override:
                cfg[key] = bool(override[key])
    return cfg


# Back-compat: some callers/tests import MIN_BRIEF_USD as the bar. It reflects
# the env/default at import; the live bar comes from watch_config()["min_usd"].
MIN_BRIEF_USD = float(os.getenv("MIN_BRIEF_USD", str(_DEFAULT_MIN_BRIEF_USD)))


def significance_score(ev: LogEvent) -> float:
    """0 = drop. Higher = more worth a brief. Ranks the curated feed too."""
    facts = getattr(ev, "facts", None)
    if facts is None:
        return 0.0
    cfg = watch_config()
    min_usd = cfg["min_usd"] or _DEFAULT_MIN_BRIEF_USD
    kind = getattr(facts, "kind", "activity")
    usd = getattr(facts, "amount_usd", None) or 0.0
    nature_switch = _NATURE_KINDS.get(kind)
    if nature_switch is not None:
        if not cfg.get(nature_switch, True):
            return 0.0  # operator turned this event type off
        # A deploy must name a real program / verb; an anonymous spam loader
        # with no resolved program is not news.
        if kind == "deploy" and not (
            getattr(facts, "program_name", "") or getattr(facts, "deploy_verb", "")
        ):
            return 0.0
        # Base 1.0 keeps these above the drop line; a dollar figure (e.g. a
        # governed treasury move) nudges them up without dwarfing real whales.
        return 1.0 + min(usd, min_usd) / min_usd
    if kind in _VALUE_MOVE_KINDS:
        return usd / min_usd if usd >= min_usd else 0.0
    # activity / unknown: the "water" the feed used to fill up with.
    return 0.0


def is_significant(ev: LogEvent) -> bool:
    """The hard gate: only events above the editorial bar reach the paid pipeline."""
    return significance_score(ev) > 0.0


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
