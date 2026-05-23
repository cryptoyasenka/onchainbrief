"""Static feed page — the public-facing output.

Scans a briefs directory (the .md + .png pairs that pipeline.py writes) and
renders one index.html with premium design. Copies all PNG cards to the output
directory so that images are not broken on deployment.
"""

from __future__ import annotations

import html
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

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
        # Generate sources list as domain badges
        sources_html = ""
        if it.sources:
            badges = []
            for s in it.sources:
                parsed = urlparse(s)
                domain = parsed.netloc.replace("www.", "")
                if not domain:
                    domain = "source"
                badges.append(
                    f'<a class="source-badge" href="{html.escape(s)}" target="_blank" rel="noopener noreferrer">{html.escape(domain)}</a>'
                )
            sources_html = (
                f'<div class="badges-container">'
                f'<div class="section-title">Sources</div>'
                f'<div class="sources-list">{"".join(badges)}</div>'
                f'</div>'
            )

        # Provenance signature
        sig = html.escape(it.signature)
        provenance_html = (
            f'<a class="proof-badge onchain" href="https://solscan.io/tx/{sig}" target="_blank" rel="noopener noreferrer">'
            f'<span class="badge-label">🔗 On-Chain Proof</span>'
            f'<span>{sig[:8]}…{sig[-8:]}</span>'
            f'</a>'
        )

        # Attestation signature
        attest_html = ""
        if it.attest_sig:
            asig = html.escape(it.attest_sig)
            acl = html.escape(it.attest_cluster)
            qs = "" if acl == "mainnet-beta" else f"?cluster={acl}"
            attest_html = (
                f'<a class="proof-badge attest" href="https://solscan.io/tx/{asig}{qs}" target="_blank" rel="noopener noreferrer">'
                f'<span class="badge-label">✓ SAP Attestation ({acl})</span>'
                f'<span>{asig[:8]}…{asig[-8:]}</span>'
                f'</a>'
            )

        cards.append(
            f'<article>'
            f'<div class="img-wrapper">'
            f'<img src="{html.escape(it.card)}" alt="{html.escape(it.headline)}">'
            f'</div>'
            f'<h2>{html.escape(it.headline)}</h2>'
            f'<p class="narrative">{html.escape(it.narrative)}</p>'
            f'{sources_html}'
            f'<div class="proofs">'
            f'{provenance_html}'
            f'{attest_html}'
            f'</div>'
            f'</article>'
        )

    body = "\n".join(cards) if cards else '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 40px 0;">No briefs yet</div>'

    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OnchainBrief — Verified Solana Intelligence</title>"
        "<style>"
        "@import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');"
        ":root{"
        "--bg:#030712;"
        "--card-bg:rgba(17,24,39,0.7);"
        "--border:rgba(255,255,255,0.08);"
        "--border-hover:rgba(255,255,255,0.16);"
        "--text:#f3f4f6;"
        "--text-muted:#9ca3af;"
        "--primary:#8b5cf6;"
        "--primary-glow:rgba(139,92,246,0.15);"
        "--success:#10b981;"
        "--success-glow:rgba(16,185,129,0.15);"
        "--accent:#06b6d4"
        "}"
        "body{"
        "background-color:var(--bg);"
        "background-image:"
        "radial-gradient(at 0% 0%,rgba(139,92,246,0.08) 0px,transparent 50%),"
        "radial-gradient(at 100% 100%,rgba(6,182,212,0.05) 0px,transparent 50%);"
        "color:var(--text);"
        "font-family:'Inter',system-ui,-apple-system,sans-serif;"
        "margin:0;"
        "padding:0;"
        "min-height:100vh;"
        "display:flex;"
        "flex-direction:column"
        "}"
        ".container{"
        "max-width:1200px;"
        "margin:0 auto;"
        "padding:60px 24px;"
        "flex-grow:1"
        "}"
        "header{"
        "text-align:center;"
        "margin-bottom:60px"
        "}"
        ".logo{"
        "display:inline-flex;"
        "align-items:center;"
        "gap:12px;"
        "margin-bottom:16px"
        "}"
        ".logo-icon{"
        "font-size:32px;"
        "background:linear-gradient(135deg,var(--primary),var(--accent));"
        "-webkit-background-clip:text;"
        "-webkit-text-fill-color:transparent;"
        "filter:drop-shadow(0 2px 8px var(--primary-glow))"
        "}"
        "h1{"
        "font-size:38px;"
        "font-weight:700;"
        "margin:0;"
        "background:linear-gradient(to right,#ffffff,#d1d5db);"
        "-webkit-background-clip:text;"
        "-webkit-text-fill-color:transparent;"
        "letter-spacing:-0.02em"
        "}"
        ".subtitle{"
        "color:var(--text-muted);"
        "font-size:16px;"
        "margin-top:12px;"
        "max-width:600px;"
        "margin-left:auto;"
        "margin-right:auto;"
        "font-weight:300;"
        "line-height:1.6"
        "}"
        ".grid{"
        "display:grid;"
        "grid-template-columns:repeat(auto-fill,minmax(360px,1fr));"
        "gap:32px"
        "}"
        "article{"
        "background:var(--card-bg);"
        "backdrop-filter:blur(12px);"
        "-webkit-backdrop-filter:blur(12px);"
        "border:1px solid var(--border);"
        "border-radius:20px;"
        "padding:24px;"
        "display:flex;"
        "flex-direction:column;"
        "transition:all 0.3s cubic-bezier(0.4,0,0.2,1);"
        "position:relative;"
        "overflow:hidden"
        "}"
        "article:hover{"
        "transform:translateY(-4px);"
        "border-color:var(--border-hover);"
        "box-shadow:0 12px 30px rgba(0,0,0,0.5),0 0 2px var(--primary)"
        "}"
        ".img-wrapper{"
        "width:100%;"
        "border-radius:12px;"
        "overflow:hidden;"
        "aspect-ratio:1.58;"
        "background:#1f2937;"
        "margin-bottom:20px;"
        "border:1px solid rgba(255,255,255,0.05)"
        "}"
        "article img{"
        "width:100%;"
        "height:100%;"
        "object-fit:cover;"
        "transition:transform 0.5s ease"
        "}"
        "article:hover img{"
        "transform:scale(1.03)"
        "}"
        "h2{"
        "font-size:20px;"
        "font-weight:600;"
        "margin:0 0 12px 0;"
        "color:#ffffff;"
        "line-height:1.4"
        "}"
        ".narrative{"
        "color:var(--text-muted);"
        "font-size:14px;"
        "line-height:1.6;"
        "margin:0 0 20px 0;"
        "flex-grow:1"
        "}"
        ".section-title{"
        "font-size:11px;"
        "text-transform:uppercase;"
        "letter-spacing:0.05em;"
        "color:var(--text-muted);"
        "margin-bottom:8px;"
        "font-weight:600"
        "}"
        ".badges-container{"
        "margin-bottom:18px"
        "}"
        ".sources-list{"
        "display:flex;"
        "flex-wrap:wrap;"
        "gap:8px"
        "}"
        ".source-badge{"
        "display:inline-flex;"
        "align-items:center;"
        "background:rgba(255,255,255,0.04);"
        "border:1px solid rgba(255,255,255,0.06);"
        "color:var(--text-muted);"
        "padding:4px 10px;"
        "border-radius:6px;"
        "font-size:11px;"
        "text-decoration:none;"
        "transition:all 0.2s ease"
        "}"
        ".source-badge:hover{"
        "background:rgba(255,255,255,0.08);"
        "color:#ffffff;"
        "border-color:rgba(255,255,255,0.12)"
        "}"
        ".proofs{"
        "display:flex;"
        "flex-direction:column;"
        "gap:8px;"
        "border-top:1px solid rgba(255,255,255,0.06);"
        "padding-top:16px;"
        "margin-top:auto"
        "}"
        ".proof-badge{"
        "display:flex;"
        "align-items:center;"
        "justify-content:space-between;"
        "padding:8px 12px;"
        "border-radius:8px;"
        "font-size:12px;"
        "text-decoration:none;"
        "transition:all 0.2s ease;"
        "font-family:monospace"
        "}"
        ".proof-badge.onchain{"
        "background:var(--primary-glow);"
        "border:1px solid rgba(139,92,246,0.2);"
        "color:#c084fc"
        "}"
        ".proof-badge.onchain:hover{"
        "background:rgba(139,92,246,0.25);"
        "border-color:rgba(139,92,246,0.4)"
        "}"
        ".proof-badge.attest{"
        "background:var(--success-glow);"
        "border:1px solid rgba(16,185,129,0.2);"
        "color:#34d399"
        "}"
        ".proof-badge.attest:hover{"
        "background:rgba(16,185,129,0.25);"
        "border-color:rgba(16,185,129,0.4)"
        "}"
        ".badge-label{"
        "font-family:'Inter',sans-serif;"
        "font-weight:500;"
        "font-size:11px"
        "}"
        "footer{"
        "text-align:center;"
        "padding:40px 24px;"
        "color:var(--text-muted);"
        "font-size:13px;"
        "border-top:1px solid var(--border);"
        "background:rgba(10,15,26,0.5);"
        "backdrop-filter:blur(8px);"
        "margin-top:80px"
        "}"
        "footer a{"
        "color:var(--primary);"
        "text-decoration:none;"
        "transition:color 0.2s ease"
        "}"
        "footer a:hover{"
        "color:var(--accent)"
        "}"
        "@media (max-width:640px){"
        ".container{"
        "padding:40px 16px"
        "}"
        ".grid{"
        "grid-template-columns:1fr"
        "}"
        "h1{"
        "font-size:30px"
        "}"
        "}"
        "</style></head><body>"
        "<div class=container>"
        "<header>"
        "<div class=logo><span class=logo-icon>⚡</span></div>"
        "<h1>OnchainBrief</h1>"
        "<p class=subtitle>Curated Solana network security & program intelligence, verified on-chain and registered via SAP.</p>"
        "</header>"
        f"<main class=grid>{body}</main>"
        "</div>"
        "<footer>"
        "<p>Powered by ACE Services & Solana. Attested via Agent PDA. <a href='https://github.com/cryptoyasenka/oobe-ace-bounty' target=_blank rel='noopener noreferrer'>View Source</a></p>"
        "</footer>"
        "</body></html>"
    )


def build_feed(briefs_dir: str | Path, out_html: str | Path) -> Path:
    briefs_dir = Path(briefs_dir)
    out_html = Path(out_html)
    out_dir = out_html.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy all PNGs from briefs_dir to out_dir
    for png_path in briefs_dir.glob("*.png"):
        shutil.copy2(png_path, out_dir / png_path.name)

    mds = sorted(
        briefs_dir.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items = [it for p in mds if (it := _parse_brief(p))]
    out_html.write_text(_render(items), encoding="utf-8")
    return out_html
