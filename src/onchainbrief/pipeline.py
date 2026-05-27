"""Brief pipeline: one notable event -> 3 distinct ACE services -> artifact."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .attest import Attestation, Attestor
from .compose import compose_card, write_brief_markdown
from .filter import categorize_event
from .watcher import LogEvent


@dataclass
class SerpResult:
    summary: str
    sources: list[str]


class BriefClient(Protocol):
    def serp(self, query: str) -> SerpResult: ...
    def chat(self, prompt: str) -> str: ...
    def image(self, prompt: str) -> bytes: ...


@dataclass
class BriefArtifacts:
    card_path: Path
    brief_path: Path
    headline: str
    narrative: str
    sources: list[str]
    signature: str
    attestation: Attestation | None = None


def _facts(ev: LogEvent):
    """The decoded EventFacts if the signature was enriched, else None."""
    return getattr(ev, "facts", None)


def _first_non_infra(program_ids: list[str]) -> str:
    """First program that isn't pure infra, else "".

    The watcher emits `program_ids` alphabetically sorted (`sorted(set(...))`),
    so `program_ids[0]` is almost always the System program `111…` or
    `ComputeBudget…` — never the entity a brief is about. Any fallback that
    needs "the program" must skip infra the same way the decoded-facts path
    does (see txfacts.INFRA_PROGRAMS), or the brief leaks "ComputeB on-chain
    move" whenever decoding is unavailable.
    """
    from .txfacts import INFRA_PROGRAMS

    return next((p for p in program_ids if p not in INFRA_PROGRAMS), "")


def _query_for(ev: LogEvent) -> str:
    """SERP query = research the ENTITY, not the raw program id.

    A program-id string returns developer tutorials (StackExchange, docs) -
    noise that produced the "water" briefs. Querying the protocol / asset by
    name returns who-and-what context worth one line of "so what".
    """
    f = _facts(ev)
    if f is not None:
        if f.kind == "deploy":
            return "Solana program deployment BPFLoaderUpgradeable significance"
        if getattr(f, "program_name", ""):
            asset = f" {f.asset}" if f.asset else ""
            return f"{f.program_name} Solana protocol{asset} overview"
        if f.asset and f.asset not in ("SOL",):
            return f"Solana token {f.asset} project overview"
    prog = _first_non_infra(ev.program_ids)
    if prog:
        return f"Solana program {prog} protocol identity"
    return "notable Solana mainnet on-chain activity"


def _headline_for(ev: LogEvent) -> str:
    """Deterministic fallback headline built from real facts when available."""
    f = _facts(ev)
    if f is not None:
        from .txfacts import _fmt_amount
        if f.kind == "deploy":
            return "PROGRAM UPGRADED" if getattr(f, "deploy_verb", "") == "upgraded" else "NEW PROGRAM DEPLOYED"
        if f.has_value and f.asset:
            verb = "SWAP" if f.kind == "swap" else (
                "MOVE" if f.kind == "whale_transfer" else "TRANSFER"
            )
            return f"{_fmt_amount(f.amount_native)} {f.asset} {verb}".upper()
        if f.kind == "governance":
            return "ON-CHAIN GOVERNANCE ACTION"
    prog = _first_non_infra(ev.program_ids)
    if prog:
        return f"{prog[:8]} on-chain move"
    return "ON-CHAIN ACTIVITY"


def _facts_block(ev: LogEvent) -> str:
    """The hard, verifiable facts handed to the LLM - the spine of the brief."""
    f = _facts(ev)
    if f is None:
        prog = _first_non_infra(ev.program_ids)
        if prog:
            return f"- Program: {prog}\n- (transaction not yet decoded)"
        return "- (transaction not yet decoded)"
    lines = [f"- Event type: {f.kind}"]
    if f.has_value and f.asset:
        amt = f"{f.amount_native:,.4f}".rstrip("0").rstrip(".")
        usd = f" (~${f.amount_usd:,.0f} USD)" if f.amount_usd else ""
        lines.append(f"- Amount: {amt} {f.asset}{usd}")
    if f.from_addr:
        lines.append(f"- From: {f.from_addr}")
    if f.to_addr:
        lines.append(f"- To: {f.to_addr}")
    # Headline program only: program_name (readable) > program_id (resolved
    # non-infra id) > nothing. Never f.programs[0] - that is usually
    # ComputeBudget/System and leaks "involving the ComputeBudget program".
    prog = getattr(f, "program_name", "") or getattr(f, "program_id", "")
    if prog:
        lines.append(f"- Program: {prog}")
    if f.kind == "deploy":
        # For a deploy/upgrade the meaningful action is the loader verb, not the
        # outer executor instruction (e.g. a Squads VaultTransactionExecute that
        # CPIs the upgrade) - feeding that raw instruction misled the brief into
        # "deployed with a VaultTransactionExecute instruction".
        lines.append(f"- Action: program {getattr(f, 'deploy_verb', '') or 'deployed'} via the BPF upgradeable loader")
    elif f.instruction:
        lines.append(f"- Instruction: {f.instruction}")
    return "\n".join(lines)


def _first_sentence(text: str) -> str:
    """Subline = the first sentence of the narrative.

    Splitting on a bare "." would truncate "$1.5M flow on Acme" to "$1".
    Period-followed-by-space is the sentence boundary that survives numbers
    like 1.5 / 3.14 / abbreviations like U.S. (treated as part of the same
    sentence - acceptable for a one-line card subline).
    """
    head = text.split(". ", 1)[0].strip()
    return head[:-1] if head.endswith(".") else head


def run_brief(
    ev: LogEvent,
    client: BriefClient,
    out_dir: str | Path,
    *,
    attestor: Attestor | None = None,
    skip_image: bool = False,
) -> BriefArtifacts:
    """Synchronous core (each ACE call is one distinct service).

    With `attestor`: artifacts are produced once and hashed for the memo. The
    memo's `sha256` binds those exact public bytes, so the files are NEVER
    mutated afterwards — the receipt (tx sig + cluster) lands in a
    `<stem>.attest.json` sidecar that the feed reads. Editing the card or
    brief post-attest would make the public verifier's recomputed hash diverge
    from the on-chain memo.
    """
    out_dir = Path(out_dir)
    serp = client.serp(_query_for(ev))

    chat_resp = client.chat(
        "You write whale-alert-grade on-chain briefs. The VERIFIED FACTS below "
        "come from the transaction itself — they are the truth; the web context "
        "is only background on the entities involved.\n\n"
        "Write:\n"
        "1. TITLE: 3-6 words, the concrete fact with the number "
        "(e.g. '5,000 SOL MOVED OFF-EXCHANGE', 'NEW PROGRAM DEPLOYED'). "
        "No generic words like 'activity' or 'intelligence'.\n"
        "2. NARRATIVE: exactly two sentences. Sentence 1 = WHAT happened, stated "
        "with the amount/asset/parties from the facts. Sentence 2 = SO WHAT, one "
        "line on why it matters, grounded in the web context. No hype, no "
        "tutorials, no restating the program's documentation.\n"
        "Use ONE consistent spelling/casing for any token symbol or ticker "
        "across both the TITLE and the NARRATIVE (do not mix e.g. AUQA and AuQa).\n\n"
        "Format exactly:\n"
        "TITLE: <title>\n"
        "NARRATIVE: <two sentences>\n\n"
        f"VERIFIED FACTS (from tx {ev.signature}):\n{_facts_block(ev)}\n\n"
        f"WEB CONTEXT (entity background only):\n{serp.summary}"
    )

    headline = _headline_for(ev)
    narrative = chat_resp

    if "TITLE:" in chat_resp and "NARRATIVE:" in chat_resp:
        try:
            parts = chat_resp.split("NARRATIVE:", 1)
            title_part = parts[0].replace("TITLE:", "").strip()
            narrative_part = parts[1].strip()
            if title_part and narrative_part:
                headline = title_part
                narrative = narrative_part
        except Exception:
            pass
    elif "\n" in chat_resp:
        lines = [l.strip() for l in chat_resp.splitlines() if l.strip()]
        if len(lines) >= 2 and (lines[0].lower().startswith("title:") or lines[0].lower().startswith("headline:")):
            try:
                headline = lines[0].split(":", 1)[1].strip()
                narrative = "\n".join(lines[1:]).replace("NARRATIVE:", "").replace("narrative:", "").strip()
            except Exception:
                pass

    image_bytes = None
    card_path = None
    card = None

    # Triage and categorize event
    category = categorize_event(ev)

    if not skip_image:
        image_bytes = client.image(
            f"Abstract modern 3D geometric concept for: {headline}. "
            "Matte clay shapes, frosted glass blocks, neutral sophisticated colors of charcoal, silver, and muted bronze. "
            "Soft clean studio lighting, high depth-of-field background blur, modern tech branding design, no text"
        )
        stem = ev.signature[:16]
        card_path = out_dir / f"{stem}.png"
        card = compose_card(
            image_bytes, headline, _first_sentence(narrative),
            ev.signature, card_path, facts=_facts(ev), category=category,
        )

    stem = ev.signature[:16]
    brief_path = out_dir / f"{stem}.md"
    brief = write_brief_markdown(
        headline, narrative, serp.sources, ev.signature,
        card, brief_path, category=category,
    )

    attestation: Attestation | None = None
    if attestor is not None:
        paths = [brief] if card is None else [card, brief]
        attestation = attestor.attest(ev.signature, paths)
        if attestation is not None:
            # Do NOT re-compose card/brief: the memo binds the bytes hashed
            # above, so the published files must stay pristine for the
            # verifier to reproduce the same sha256. Record the receipt in a
            # sidecar the feed reads instead of editing attested artifacts.
            sidecar = brief_path.with_suffix(".attest.json")
            sidecar.write_text(
                json.dumps(
                    {
                        "tx_sig": attestation.tx_sig,
                        "cluster": attestation.cluster,
                        "payload_sha256": attestation.payload_sha256,
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )

    art = BriefArtifacts(
        card_path=card,
        brief_path=brief,
        headline=headline,
        narrative=narrative,
        sources=serp.sources,
        signature=ev.signature,
        attestation=attestation,
    )

    try:
        send_push_notification(art)
    except Exception as e:
        print(f"[PUSH-ERROR] {e!r}")

    return art


def send_push_notification(art: BriefArtifacts) -> None:
    """Dispatches alerts to Telegram / Discord if configured in environment."""
    import os
    import requests

    # Telegram
    tg_token = os.getenv("TELEGRAM_BOT_TOKEN")
    tg_chat = os.getenv("TELEGRAM_CHAT_ID")
    if tg_token and tg_chat:
        try:
            caption = f"⚡ *{art.headline}*\n\n{art.narrative}\n\n🔗 [Solana tx](https://solscan.io/tx/{art.signature})"
            if art.attestation:
                qs = "" if art.attestation.cluster == "mainnet-beta" else f"?cluster={art.attestation.cluster}"
                caption += f"\n✓ [SAP Attestation](https://solscan.io/tx/{art.attestation.tx_sig}{qs})"

            if art.card_path and Path(art.card_path).exists():
                url = f"https://api.telegram.org/bot{tg_token}/sendPhoto"
                with open(art.card_path, "rb") as f:
                    r = requests.post(
                        url,
                        data={"chat_id": tg_chat, "caption": caption, "parse_mode": "Markdown"},
                        files={"photo": f},
                        timeout=15
                    )
                r.raise_for_status()
            else:
                url = f"https://api.telegram.org/bot{tg_token}/sendMessage"
                r = requests.post(
                    url,
                    json={"chat_id": tg_chat, "text": caption, "parse_mode": "Markdown", "disable_web_page_preview": True},
                    timeout=15
                )
                r.raise_for_status()
            print("[PUSH] Sent Telegram notification successfully.")
        except Exception as e:
            print(f"[PUSH] Telegram notification failed: {e!r}")

    # Discord webhook
    discord_url = os.getenv("DISCORD_WEBHOOK_URL")
    if discord_url:
        try:
            embed = {
                "title": art.headline,
                "description": art.narrative,
                "color": 9133302, # #8b5cf6 primary purple
                "fields": [
                    {"name": "Solana Transaction", "value": f"[Link](https://solscan.io/tx/{art.signature})", "inline": True}
                ],
                "footer": {"text": "OnchainBrief — on-chain event briefs"}
            }
            if art.attestation:
                qs = "" if art.attestation.cluster == "mainnet-beta" else f"?cluster={art.attestation.cluster}"
                embed["fields"].append({"name": "SAP Attestation", "value": f"[Link](https://solscan.io/tx/{art.attestation.tx_sig}{qs})", "inline": True})

            r = requests.post(discord_url, json={"embeds": [embed]}, timeout=15)
            r.raise_for_status()
            print("[PUSH] Sent Discord notification successfully.")
        except Exception as e:
            print(f"[PUSH] Discord notification failed: {e!r}")


async def run_brief_async(
    ev: LogEvent,
    client: BriefClient,
    out_dir: str | Path,
    *,
    attestor: Attestor | None = None,
    skip_image: bool = False,
) -> BriefArtifacts:
    # ACE calls are blocking HTTP; thread off the event loop so the watcher
    # keeps draining the WS while a brief is produced.
    import asyncio

    return await asyncio.to_thread(
        run_brief, ev, client, out_dir, attestor=attestor, skip_image=skip_image
    )
