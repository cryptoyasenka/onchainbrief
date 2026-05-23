"""Brief pipeline: one notable event -> 3 distinct ACE services -> artifact.

Coordinates querying SERP, generating brief narratives via chat LLM, and 
composing the final visual trading card image.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .attest import Attestation, Attestor
from .compose import compose_card, write_brief_markdown
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


def _query_for(ev: LogEvent) -> str:
    prog = ev.program_ids[0] if ev.program_ids else "unknown program"
    return f"Solana program {prog} recent activity context"


def _headline_for(ev: LogEvent) -> str:
    prog = ev.program_ids[0] if ev.program_ids else "Solana"
    return f"{prog[:8]} on-chain move"


def _first_sentence(text: str) -> str:
    """Subline = the first sentence of the narrative.

    Splitting on a bare "." would truncate "$1.5M flow on Acme" to "$1".
    Period-followed-by-space is the sentence boundary that survives numbers
    like 1.5 / 3.14 / abbreviations like U.S. (treated as part of the same
    sentence — acceptable for a one-line card subline).
    """
    head = text.split(". ", 1)[0].strip()
    return head[:-1] if head.endswith(".") else head


def run_brief(
    ev: LogEvent,
    client: BriefClient,
    out_dir: str | Path,
    *,
    attestor: Attestor | None = None,
) -> BriefArtifacts:
    """Synchronous core (each ACE call is one distinct service).

    With `attestor`: artifacts are produced once, hashed for the memo, then
    re-composed with the attestation link rendered onto the card and brief.
    The memo's `sha256` fixes the *unattested* artifacts (the moment of
    generation); the public-facing files then carry the receipt link.
    """
    out_dir = Path(out_dir)
    serp = client.serp(_query_for(ev))

    narrative = client.chat(
        "Write a tight 3-sentence market brief from this context. No hype, "
        f"facts only.\n\nEvent sig: {ev.signature}\nContext:\n{serp.summary}"
    )

    headline = _headline_for(ev)
    image_bytes = client.image(
        f"minimal flat-design trading card visual for: {headline}; "
        "clean, editorial, no text"
    )

    stem = ev.signature[:16]
    card_path = out_dir / f"{stem}.png"
    brief_path = out_dir / f"{stem}.md"
    card = compose_card(
        image_bytes, headline, _first_sentence(narrative),
        ev.signature, card_path,
    )
    brief = write_brief_markdown(
        headline, narrative, serp.sources, ev.signature,
        card, brief_path,
    )

    attestation: Attestation | None = None
    if attestor is not None:
        attestation = attestor.attest(ev.signature, [card, brief])
        if attestation is not None:
            tag = (attestation.tx_sig, attestation.cluster)
            card = compose_card(
                image_bytes, headline, _first_sentence(narrative),
                ev.signature, card_path, attestation=tag,
            )
            brief = write_brief_markdown(
                headline, narrative, serp.sources, ev.signature,
                card, brief_path, attestation=tag,
            )

    return BriefArtifacts(
        card_path=card,
        brief_path=brief,
        headline=headline,
        narrative=narrative,
        sources=serp.sources,
        signature=ev.signature,
        attestation=attestation,
    )


async def run_brief_async(
    ev: LogEvent,
    client: BriefClient,
    out_dir: str | Path,
    *,
    attestor: Attestor | None = None,
) -> BriefArtifacts:
    # ACE calls are blocking HTTP; thread off the event loop so the watcher
    # keeps draining the WS while a brief is produced.
    import asyncio

    return await asyncio.to_thread(run_brief, ev, client, out_dir, attestor=attestor)
