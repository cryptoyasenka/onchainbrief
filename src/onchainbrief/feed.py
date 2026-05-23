"""Static feed page — the public-facing output.

Scans a briefs directory (the .md + .png pairs that pipeline.py writes) and
renders ONE self-contained index.html (inline CSS, zero external deps) so it
serves as a plain static site on Railway (not Vercel). Newest
brief first by file mtime.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from pathlib import Path

_CARD_RE = re.compile(r"!\[card\]\((.+?)\)")
_SIG_RE = re.compile(r"Solana tx `([^`]+)`")
_ATTEST_RE = re.compile(r"Attestation tx `([^`]+)` \(([^)]+)\)")
_SRC_RE = re.compile(r"^- (\S+)", re.MULTILINE)


@dataclass
class FeedItem:
    headline: str
    card: str
    narrative: str
    signature: str
    sources: list[str]
    attest_sig: str = ""
    attest_cluster: str = ""


def _parse_brief(md_path: Path) -> FeedItem | None:
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    headline = next(
        (ln[2:].strip() for ln in lines if ln.startswith("# ")), ""
    )
    cm = _CARD_RE.search(text)
    sm = _SIG_RE.search(text)
    am = _ATTEST_RE.search(text)
    if not headline or not cm:
        return None
    body = text.split("\n## Sources")[0]
    body = _CARD_RE.sub("", body).split(headline, 1)[-1]
    narrative = body.replace("#", "").strip()
    src_block = text.split("## Sources")[-1].split("## Provenance")[0]
    sources = [s for s in _SRC_RE.findall(src_block) if s.startswith("http")]
    return FeedItem(
        headline=headline,
        card=cm.group(1),
        narrative=narrative,
        signature=sm.group(1) if sm else "",
        sources=sources,
        attest_sig=am.group(1) if am else "",
        attest_cluster=am.group(2) if am else "",
    )


def _render(items: list[FeedItem]) -> str:
    cards = []
    for it in items:
        srcs = "".join(
            f'<a href="{html.escape(s)}">{html.escape(s)}</a> '
            for s in it.sources
        )
        sig = html.escape(it.signature)
        attest_html = ""
        if it.attest_sig:
            asig = html.escape(it.attest_sig)
            acl = html.escape(it.attest_cluster)
            qs = "" if acl == "mainnet-beta" else f"?cluster={acl}"
            attest_html = (
                f'<a class="attest" href="https://solscan.io/tx/{asig}{qs}">'
                f"attest ({acl}): {asig[:24]}…</a>"
            )
        cards.append(
            f'<article><img src="{html.escape(it.card)}" alt="">'
            f"<h2>{html.escape(it.headline)}</h2>"
            f"<p>{html.escape(it.narrative)}</p>"
            f'<div class="src">{srcs}</div>'
            f'<a class="prov" href="https://solscan.io/tx/{sig}">'
            f"on-chain: {sig[:24]}…</a>"
            f"{attest_html}</article>"
        )
    body = "\n".join(cards) or "<p>No briefs yet</p>"
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OnchainBrief</title><style>"
        "body{background:#0a0d12;color:#e6eaf2;font:16px/1.6 system-ui,sans-serif;"
        "margin:0;padding:48px 16px}.wrap{max-width:760px;margin:0 auto}"
        "h1{font-size:28px;letter-spacing:.02em}"
        "article{background:#11151c;border:1px solid #1d242e;border-radius:14px;"
        "padding:18px;margin:22px 0}article img{width:100%;border-radius:8px}"
        "h2{font-size:20px;margin:14px 0 6px}.src a{color:#7fb4ff;font-size:13px;"
        "word-break:break-all}.prov,.attest{display:inline-block;margin-top:10px;"
        "color:#8bd;font-size:13px;text-decoration:none}"
        ".attest{margin-left:14px;color:#8cdcb4}"
        "</style></head><body><div class=wrap>"
        "<h1>OnchainBrief</h1>"
        "<p>Notable Solana events, briefed and attested on-chain</p>"
        f"{body}</div></body></html>"
    )


def build_feed(briefs_dir: str | Path, out_html: str | Path) -> Path:
    briefs_dir = Path(briefs_dir)
    mds = sorted(
        briefs_dir.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items = [it for p in mds if (it := _parse_brief(p))]
    out_html = Path(out_html)
    out_html.parent.mkdir(parents=True, exist_ok=True)
    out_html.write_text(_render(items), encoding="utf-8")
    return out_html
