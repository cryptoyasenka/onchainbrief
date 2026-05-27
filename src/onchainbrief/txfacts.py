"""Decode the real event out of a Solana transaction.

logsSubscribe only gives a signature + log lines, not the event itself. To
say what happened (amount, asset, from/to, program) we fetch the full tx once
via getTransaction and read it: lamport deltas for SOL moves, pre/post token
balances for SPL moves, the program set for deploys/swaps.

extract_facts is pure (takes a decoded result dict) so it is testable with a
saved fixture and never touches the network. enrich_event does the RPC
round-trip and attaches the result to a LogEvent.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import requests

LAMPORTS_PER_SOL = 1_000_000_000

# Mints whose UI amount is, by definition, the USD figure.
STABLECOINS = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
}
WRAPPED_SOL = "So11111111111111111111111111111111111111112"

BPF_UPGRADEABLE = "BPFLoaderUpgradeab1e11111111111111111111111"
BPF_LOADERS = {
    BPF_UPGRADEABLE,
    "BPFLoader2111111111111111111111111111111111",
    "BPFLoader1111111111111111111111111111111111",
}

# Known programs → readable name. Used both to classify swaps and to give the
# SERP step a real entity to research (instead of a bare program-id string).
KNOWN_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4": "Jupiter Aggregator",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB": "Jupiter Aggregator v4",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY": "Phoenix DEX",
    "dRiftyHA39MWEi3m9aunc5MzRF1JYuBsbn6VPcozatx": "Drift Protocol",
    "MERLuDFBMmsHnsBPZw2sDQZHvXFMwp8EdjudcU2HKky": "Mercurial",
    "metaqbxxUerdq28cj1RbAWkYQm3ybzjb6a8bt518x1s": "Metaplex Token Metadata",
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA": "SPL Token",
    "SQDS4ep65T869zMMBKyuUq6aD6EgTu8psMjkvj52pCf": "Squads Protocol",
    "ComputeBudget111111111111111111111111111111": "Compute Budget",
    # SPL Governance (Realms). Also in KNOWN_GOVERNANCE below for the kind=
    # classifier; listed here too so it can WIN the headline-program pick - a
    # governance tx is usually [ComputeBudget, GovER…] and ComputeBudget is
    # infra, so without this entry program_name stays empty and the brief
    # leaks "involving the ComputeBudget program".
    "GovER5Lthms3bLBqWub97yVrMmEogzX7xNjdXpPPCVZw": "SPL Governance",
}
SWAP_PROGRAMS = {
    "JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4",
    "JUP4Fb2cqiRUcaTHdrPC8h2gNsA2ETXiPDD33WcGuJB",
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
    "PhoeNiXZ8ByJGLkxNfZRnkUfjvmuYqLR89jjFHGqdXY",
}
GOVERNANCE_HINTS = (
    "propose", "proposal", "vote", "voted", "council", "governance",
)
# SPL Governance (Realms). Its instruction logs are often program-internal and
# omit words like "vote"/"proposal", so a governance tx that moves no token/SOL
# would fall through to "activity". Matching the program id catches those; a
# miss just falls back to the log-hint check below.
KNOWN_GOVERNANCE = {
    "GovER5Lthms3bLBqWub97yVrMmEogzX7xNjdXpPPCVZw",  # SPL Governance v3
}

SPL_TOKEN_PROGRAM = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
# SPL-Token instruction types that change control over a mint/account rather
# than move value: who can mint/freeze (setAuthority), freezing/unfreezing an
# account (freeze/thaw), and pulling a delegate (revoke). These are the events
# the "Security" feed tab is for. Checked only after value-moving classifiers,
# so a swap that happens to revoke a delegate still classifies by its trade.
_SECURITY_IX_TYPES = {"setauthority", "freezeaccount", "thawaccount", "revoke"}

# Utility/infra programs invoked by almost every transaction. They are never
# "the entity" a brief is about (ComputeBudget sets the fee at the top of most
# txs, System/SPL-Token are plumbing), so they must not win the headline-program
# pick - otherwise every brief reads "via Compute Budget".
INFRA_PROGRAMS = {
    "ComputeBudget111111111111111111111111111111",
    "11111111111111111111111111111111",  # System program
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",  # SPL Token
    "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",  # Associated Token Account
    *BPF_LOADERS,
}


@dataclass
class EventFacts:
    """The decoded event - the fact a brief is built around."""

    kind: str = "activity"  # whale_transfer|token_transfer|swap|deploy|security|governance|activity
    summary: str = ""  # one-line whale-alert-style fact
    amount_native: float | None = None  # SOL or token UI amount
    amount_usd: float | None = None
    asset: str = ""  # "SOL" | "USDC" | mint[:4]…
    from_addr: str = ""
    to_addr: str = ""
    programs: list[str] = field(default_factory=list)
    program_name: str = ""  # readable name of the headline program, if known
    program_id: str = ""  # resolved headline program id (never pure infra)
    deploy_verb: str = ""  # "deployed" | "upgraded" when kind == "deploy"
    instruction: str = ""  # primary "Instruction: X" from logs
    fee_lamports: int | None = None
    block_time: int | None = None

    @property
    def has_value(self) -> bool:
        return self.amount_native is not None and self.amount_native > 0


def _short(addr: str) -> str:
    return f"{addr[:4]}…{addr[-4:]}" if len(addr) > 10 else addr


def _fmt_amount(x: float) -> str:
    if x >= 1_000_000:
        return f"{x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"{x / 1_000:.2f}K"
    if x >= 1:
        return f"{x:,.2f}"
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _fmt_usd(x: float) -> str:
    if x >= 1_000_000:
        return f"${x / 1_000_000:.2f}M"
    if x >= 1_000:
        return f"${x / 1_000:.1f}K"
    return f"${x:,.0f}"


def _account_keys(message: dict) -> list[str]:
    keys = message.get("accountKeys", []) or []
    out: list[str] = []
    for k in keys:
        if isinstance(k, str):
            out.append(k)
        elif isinstance(k, dict):
            pk = k.get("pubkey")
            if pk:
                out.append(str(pk))
    return out


def _program_ids(result: dict, account_keys: list[str]) -> list[str]:
    """Programs invoked, from logs first (jsonParsed) then instruction list."""
    progs: list[str] = []
    for ln in (result.get("meta", {}) or {}).get("logMessages", []) or []:
        if ln.startswith("Program ") and " invoke " in ln:
            parts = ln.split()
            if len(parts) >= 2:
                progs.append(parts[1])
    if not progs:
        msg = (result.get("transaction", {}) or {}).get("message", {}) or {}
        for ix in msg.get("instructions", []) or []:
            if isinstance(ix, dict):
                pid = ix.get("programId")
                if pid:
                    progs.append(str(pid))
                elif isinstance(ix.get("programIdIndex"), int):
                    idx = ix["programIdIndex"]
                    if 0 <= idx < len(account_keys):
                        progs.append(account_keys[idx])
    # de-dupe, preserve order
    seen: set[str] = set()
    return [p for p in progs if not (p in seen or seen.add(p))]


def _top_level_programs(result: dict) -> list[str]:
    """Programs invoked at the TOP level (CPI depth 1).

    `_program_ids` collects every invoked program incl. inner CPIs. For deploy
    detection that conflates a real top-level deploy with a higher-level program
    (e.g. a Squads multisig) that merely CPIs into the loader to execute someone
    else's upgrade. Solana logs the depth as `Program <id> invoke [N]`; N==1 is
    the top level, the actual intent of the transaction.
    """
    out: list[str] = []
    for ln in (result.get("meta", {}) or {}).get("logMessages", []) or []:
        if ln.startswith("Program ") and " invoke [1]" in ln:
            parts = ln.split()
            if len(parts) >= 2:
                out.append(parts[1])
    return out


def _deploy_target(result: dict) -> tuple[str, str] | None:
    """Parse the BPF upgradeable loader's own success log for the real program.

    The loader emits `Deployed program <id>` on a fresh deploy and
    `Upgraded program <id>` on an upgrade - even when invoked via CPI (e.g. a
    Squads multisig executing an upgrade). Returns (verb, program_id) with verb
    in {"deployed","upgraded"}, else None. This names the program that was
    actually deployed/upgraded, not the executor that triggered it.
    """
    for ln in (result.get("meta", {}) or {}).get("logMessages", []) or []:
        for marker, verb in (("Deployed program ", "deployed"),
                             ("Upgraded program ", "upgraded")):
            if marker in ln:
                pid = ln.split(marker, 1)[1].strip().split()[0]
                if pid:
                    return verb, pid
    return None


def _security_op(result: dict) -> str:
    """Return the SPL-Token authority/freeze instruction type if present, else ""."""
    msg = (result.get("transaction", {}) or {}).get("message", {}) or {}
    groups = [msg.get("instructions", []) or []]
    for inner in (result.get("meta", {}) or {}).get("innerInstructions", []) or []:
        groups.append(inner.get("instructions", []) or [])
    for ixs in groups:
        for ix in ixs:
            if not isinstance(ix, dict) or ix.get("programId") != SPL_TOKEN_PROGRAM:
                continue
            parsed = ix.get("parsed")
            t = parsed.get("type", "") if isinstance(parsed, dict) else ""
            if t.lower() in _SECURITY_IX_TYPES:
                return t
    return ""


def _primary_instruction(result: dict) -> str:
    for ln in (result.get("meta", {}) or {}).get("logMessages", []) or []:
        marker = "Program log: Instruction: "
        if marker in ln:
            return ln.split(marker, 1)[1].strip()
    return ""


def _governance_action(result: dict) -> str:
    """SPL-Governance instruction name (e.g. 'SignOffProposal'), else "".

    Realms logs the action as 'GOVERNANCE-INSTRUCTION: <Name>', a different
    marker from the generic 'Program log: Instruction: ' that _primary_instruction
    matches - so a governance tx otherwise carries no instruction at all.
    """
    for ln in (result.get("meta", {}) or {}).get("logMessages", []) or []:
        marker = "GOVERNANCE-INSTRUCTION: "
        if marker in ln:
            return ln.split(marker, 1)[1].strip()
    return ""


def _sol_move(result: dict, keys: list[str]) -> tuple[float, str, str] | None:
    """Largest net SOL move: (amount_sol, from, to). Ignores the fee on the payer."""
    meta = result.get("meta", {}) or {}
    pre = meta.get("preBalances") or []
    post = meta.get("postBalances") or []
    fee = meta.get("fee") or 0
    n = min(len(pre), len(post), len(keys))
    if n == 0:
        return None
    deltas = []
    for i in range(n):
        d = post[i] - pre[i]
        if i == 0:  # fee payer - add the fee back to see the real intent
            d += fee
        deltas.append(d)
    recv_i = max(range(n), key=lambda i: deltas[i])
    send_i = min(range(n), key=lambda i: deltas[i])
    amount = deltas[recv_i] / LAMPORTS_PER_SOL
    if amount <= 0:
        return None
    return amount, keys[send_i], keys[recv_i]


def _token_move(result: dict) -> tuple[float, str, str, str] | None:
    """Largest SPL move: (ui_amount, mint, from_owner, to_owner)."""
    meta = result.get("meta", {}) or {}
    pre = meta.get("preTokenBalances") or []
    post = meta.get("postTokenBalances") or []
    if not post and not pre:
        return None

    def _amt(b: dict) -> float:
        # uiAmount is the convenient float, but Solana RPC returns it null for
        # some mints/encodings - fall back to the string form, then to the raw
        # integer amount scaled by decimals, before giving up on the move.
        t = b.get("uiTokenAmount") or {}
        ui = t.get("uiAmount")
        if ui is not None:
            return float(ui)
        s = t.get("uiAmountString")
        if s:
            try:
                return float(s)
            except (TypeError, ValueError):
                pass
        raw, dec = t.get("amount"), t.get("decimals")
        if raw is not None and dec is not None:
            try:
                return float(int(raw)) / (10 ** int(dec))
            except (TypeError, ValueError):
                pass
        return 0.0

    bal: dict[tuple[str, str], float] = {}  # (owner, mint) -> delta
    for b in pre:
        key = (b.get("owner", ""), b.get("mint", ""))
        bal[key] = bal.get(key, 0.0) - _amt(b)
    for b in post:
        key = (b.get("owner", ""), b.get("mint", ""))
        bal[key] = bal.get(key, 0.0) + _amt(b)
    if not bal:
        return None
    (recv_owner, mint), amount = max(bal.items(), key=lambda kv: kv[1])
    if amount <= 0:
        return None
    senders = [o for (o, m), d in bal.items() if m == mint and d < 0]
    send_owner = min(
        ((o, m) for (o, m) in bal if m == mint and bal[(o, m)] < 0),
        key=lambda k: bal[k],
        default=("", mint),
    )[0] if senders else ""
    return amount, mint, send_owner, recv_owner


def extract_facts(result: dict, *, sol_price_usd: float | None = None) -> EventFacts:
    """Pure: decode a getTransaction `result` into an EventFacts."""
    if not isinstance(result, dict):
        return EventFacts()
    msg = (result.get("transaction", {}) or {}).get("message", {}) or {}
    keys = _account_keys(msg)
    programs = _program_ids(result, keys)
    meta = result.get("meta", {}) or {}
    facts = EventFacts(
        programs=programs,
        instruction=_primary_instruction(result),
        fee_lamports=meta.get("fee"),
        block_time=result.get("blockTime"),
    )
    # Headline entity = the first KNOWN protocol that isn't pure infra; only
    # fall back to a non-infra unknown program; never to ComputeBudget/System.
    headline_prog = next(
        (p for p in programs if p in KNOWN_PROGRAMS and p not in INFRA_PROGRAMS), ""
    )
    if not headline_prog:
        headline_prog = next((p for p in programs if p not in INFRA_PROGRAMS), "")
    facts.program_id = headline_prog
    facts.program_name = KNOWN_PROGRAMS.get(headline_prog, "")

    # A deploy is signalled by the loader's own `Deployed/Upgraded program <id>`
    # log (authoritative, names the real program even via a Squads CPI) OR by a
    # BPF loader invoked at the TOP level. Merely seeing a loader anywhere in the
    # invoke set is NOT a deploy - e.g. a Squads `VaultTransactionExecute` CPIs
    # into the loader to upgrade some OTHER program; the headline must name that
    # program and say "upgraded", not call the multisig executor a new deploy.
    top_level = _top_level_programs(result)
    deploy = _deploy_target(result)
    is_deploy = deploy is not None or any(p in BPF_LOADERS for p in top_level)
    is_swap = any(p in SWAP_PROGRAMS for p in programs)
    token = _token_move(result)
    sol = _sol_move(result, keys)

    if is_deploy:
        facts.kind = "deploy"
        if deploy is not None:
            verb, target = deploy
            facts.deploy_verb = verb
            facts.program_id = target
            facts.program_name = KNOWN_PROGRAMS.get(target, "")
            facts.summary = f"Program {target} {verb} on Solana mainnet"
        else:
            facts.deploy_verb = "deployed"
            facts.summary = "New program deployed on Solana mainnet"
        return facts

    # Prefer a token move (carries an asset + amount); fall back to SOL.
    if token and token[0] > 0:
        amount, mint, frm, to = token
        symbol = STABLECOINS.get(mint) or (
            "SOL" if mint == WRAPPED_SOL else f"{mint[:4]}…"
        )
        facts.amount_native = amount
        facts.asset = symbol
        facts.from_addr, facts.to_addr = frm, to
        if mint in STABLECOINS:
            facts.amount_usd = amount
        elif mint == WRAPPED_SOL and sol_price_usd:
            facts.amount_usd = amount * sol_price_usd
        verb = "routed" if is_swap else "moved"
        facts.kind = "swap" if is_swap else "token_transfer"
        usd = f" ({_fmt_usd(facts.amount_usd)})" if facts.amount_usd else ""
        where = f" via {facts.program_name}" if facts.program_name else ""
        facts.summary = (
            f"{_fmt_amount(amount)} {symbol}{usd} {verb}{where}"
        )
        return facts

    if sol and sol[0] > 0:
        amount, frm, to = sol
        facts.amount_native = amount
        facts.asset = "SOL"
        facts.from_addr, facts.to_addr = frm, to
        if sol_price_usd:
            facts.amount_usd = amount * sol_price_usd
        facts.kind = "whale_transfer"
        usd = f" ({_fmt_usd(facts.amount_usd)})" if facts.amount_usd else ""
        facts.summary = (
            f"{_fmt_amount(amount)} SOL{usd} moved "
            f"{_short(frm)} → {_short(to)}"
        )
        return facts

    sec_op = _security_op(result)
    if sec_op:
        facts.kind = "security"
        label = {
            "setauthority": "Token authority changed",
            "freezeaccount": "Token account frozen",
            "thawaccount": "Token account unfrozen",
            "revoke": "Token delegate revoked",
        }.get(sec_op.lower(), "Token control action")
        facts.summary = f"{label} via SPL Token program"
        return facts

    logs_lower = "\n".join(meta.get("logMessages", []) or []).lower()
    is_governance = any(p in KNOWN_GOVERNANCE for p in programs)
    if is_governance or any(h in logs_lower for h in GOVERNANCE_HINTS):
        facts.kind = "governance"
        action = _governance_action(result)
        if action and not facts.instruction:
            facts.instruction = action
        prefix = facts.program_name or "Governance"
        facts.summary = (
            f"{prefix}: {action} recorded on-chain"
            if action
            else f"{prefix} action recorded on-chain"
        )
        return facts

    facts.kind = "activity"
    name = facts.program_name or (_short(headline_prog) if headline_prog else "a program")
    facts.summary = f"On-chain interaction with {name}"
    return facts


def fetch_transaction(sig: str, rpc_url: str, *, timeout: float = 20.0) -> dict | None:
    """Fetch a single confirmed transaction (jsonParsed). None on miss/error."""
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "getTransaction",
        "params": [
            sig,
            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
        ],
    }
    try:
        r = requests.post(rpc_url, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json().get("result")
    except (requests.RequestException, ValueError):
        return None


_SOL_PRICE_CACHE: dict[str, float] = {}


def fetch_sol_price_usd(*, timeout: float = 8.0, ttl_seconds: int = 300) -> float | None:
    """Live SOL/USD, cached for `ttl_seconds`. None on any failure.

    Never hardcoded - if the price feed is unreachable we return None and the
    USD figure is simply omitted from the brief rather than invented.
    """
    import time

    now = time.time()
    cached = _SOL_PRICE_CACHE.get("price")
    cached_at = _SOL_PRICE_CACHE.get("at", 0.0)
    if cached is not None and (now - cached_at) < ttl_seconds:
        return cached
    try:
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "solana", "vs_currencies": "usd"},
            timeout=timeout,
        )
        r.raise_for_status()
        price = float(r.json()["solana"]["usd"])
        if price > 0:
            _SOL_PRICE_CACHE["price"] = price
            _SOL_PRICE_CACHE["at"] = now
            return price
    except (requests.RequestException, ValueError, KeyError, TypeError):
        pass
    return cached  # stale-but-real beats None when the feed blips; else None


def enrich_event(
    ev,
    rpc_url: str,
    *,
    tx_rpc_url: str | None = None,
    sol_price_usd: float | None = None,
):
    """Attach EventFacts to a LogEvent in place; returns the same event.

    Best-effort: a failed fetch leaves `ev.facts` as None and the pipeline
    falls back to its log-only behaviour. When `sol_price_usd` is not supplied
    a live price is fetched (cached) so SOL moves get a USD figure; if that
    feed is down too, USD is simply omitted - never invented.

    `tx_rpc_url` overrides where the *getTransaction* round-trip goes. The
    primary RPC (`rpc_url`, e.g. OOBE Synapse) handles light calls elsewhere,
    but OOBE's free tier doesn't serve getTransaction - so the watcher/demo
    point `tx_rpc_url` at a keyless archival RPC for the decode while light
    calls keep hitting the primary. Defaults to `rpc_url` when unset.
    """
    result = fetch_transaction(ev.signature, tx_rpc_url or rpc_url)
    if result is not None:
        if sol_price_usd is None:
            sol_price_usd = fetch_sol_price_usd()
        ev.facts = extract_facts(result, sol_price_usd=sol_price_usd)
    return ev
