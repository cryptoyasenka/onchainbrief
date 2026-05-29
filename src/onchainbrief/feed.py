"""Static feed page — the public-facing output.

Scans a briefs directory (the .md + .png pairs that pipeline.py writes) and
renders one index.html with premium design. Copies all PNG cards to the output
directory so that images are not broken on deployment.
"""

from __future__ import annotations

import html
import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import (
    DEVNET_RPC_URL,
    MAINNET_RPC_URL,
    PAYMENT_CLUSTER,
    PAYMENT_RPC_URL,
    REQUEST_BRIEF_LAMPORTS,
    lamports_to_sol_str,
)
from .filter import watch_config

_CARD_RE = re.compile(r"!\[card\]\((.+?)\)")
# The brief narrative emits the decoded dollar size of a value-move as
# "(~$N USD)". That parenthetical is the single source the client-side amount
# slider reads (via the card's data-usd attribute). Nature events
# (deploy/governance/security) carry no such figure and are never amount-filtered.
_USD_RE = re.compile(r"~\$([\d,]+(?:\.\d+)?)\s*USD")
_SIG_RE = re.compile(r"Solana tx `([^`]+)`")
_ATTEST_RE = re.compile(r"Attestation tx `([^`]+)` \(([^)]+)\)")
_SRC_RE = re.compile(r"^- (\S+)", re.MULTILINE)
_CAT_RE = re.compile(r"Category: (\S+)")
_HEADLINE_ACRONYMS = {
    "ACE",
    "BPF",
    "CSP",
    "DEX",
    "RPC",
    "SAP",
    "SOL",
    "USDC",
    "USDS",
}
_HEADLINE_TITLE_WORDS = {
    "JUPITER": "Jupiter",
    "PHANTOM": "Phantom",
    "SOLANA": "Solana",
}

# On-chain SAP agent identity (mirrors proof.AGENT_PDA / README). Used by the
# proof-ladder's "agent on-chain" link. Kept as a local literal to avoid a
# feed<->proof import cycle (proof.py imports _parse_brief from this module).
_AGENT_PDA = "DsTZa5xY4sF8y3JFdE53B8T9xEsYtvntEUggm6FwMgVi"
_AGENT_EXPLORER = f"https://explorer.solana.com/address/{_AGENT_PDA}"


@dataclass
class FeedItem:
    headline: str
    card: str
    narrative: str
    signature: str
    sources: list[str]
    category: str = "Activity"
    attest_sig: str = ""
    attest_cluster: str = ""
    md_name: str = ""
    amount_usd: float = 0.0  # decoded value-move size; 0 = nature event (no $ figure)


def _display_headline(headline: str) -> str:
    """Keep attested markdown intact while making feed headlines less shouty."""
    words = []
    for idx, raw in enumerate(headline.split()):
        leading = raw[: len(raw) - len(raw.lstrip("([{"))]
        trailing = raw[len(raw.rstrip(".,:;!?)]}…")) :]
        core = raw[len(leading) : len(raw) - len(trailing) if trailing else len(raw)]
        if core in _HEADLINE_TITLE_WORDS:
            words.append(f"{leading}{_HEADLINE_TITLE_WORDS[core]}{trailing}")
            continue
        if not core or core in _HEADLINE_ACRONYMS or not core.isupper():
            words.append(raw)
            continue
        lowered = core.lower()
        if idx == 0:
            lowered = lowered.capitalize()
        words.append(f"{leading}{lowered}{trailing}")
    return " ".join(words)


def _parse_brief(md_path: Path) -> FeedItem | None:
    text = md_path.read_text(encoding="utf-8")
    lines = text.splitlines()
    headline = next(
        (ln[2:].strip() for ln in lines if ln.startswith("# ")), ""
    )
    cm = _CARD_RE.search(text)
    sm = _SIG_RE.search(text)
    am = _ATTEST_RE.search(text)
    cat_match = _CAT_RE.search(text)
    if not headline:
        return None
    body = text.split("\n## Sources")[0]
    body = _CARD_RE.sub("", body).split(headline, 1)[-1]
    narrative = body.replace("#", "").strip()
    usd_match = _USD_RE.search(text)
    amount_usd = 0.0
    if usd_match:
        try:
            amount_usd = float(usd_match.group(1).replace(",", ""))
        except ValueError:
            amount_usd = 0.0
    src_block = text.split("## Sources")[-1].split("## Provenance")[0]
    sources = [s for s in _SRC_RE.findall(src_block) if s.startswith("http")]
    # Attestation now lives in a sidecar so the published md stays byte-for-byte
    # what the on-chain memo hashed. Fall back to the legacy in-md regex for
    # briefs written before the pristine-artifact change.
    attest_sig = am.group(1) if am else ""
    attest_cluster = am.group(2) if am else ""
    sidecar = md_path.with_suffix(".attest.json")
    if sidecar.exists():
        try:
            data = json.loads(sidecar.read_text(encoding="utf-8"))
            attest_sig = data.get("tx_sig", attest_sig) or attest_sig
            attest_cluster = data.get("cluster", attest_cluster) or attest_cluster
        except (ValueError, OSError):
            pass
    return FeedItem(
        headline=headline,
        card=cm.group(1) if cm else "",
        narrative=narrative,
        signature=sm.group(1) if sm else "",
        sources=sources,
        category=cat_match.group(1) if cat_match else "Activity",
        attest_sig=attest_sig,
        attest_cluster=attest_cluster,
        md_name=md_path.name,
        amount_usd=amount_usd,
    )


def _watch_badge_html(present_cats: set[str] | None = None) -> str:
    """Read-only banner: the agent's watch policy (what it is configured to brief on).

    Sourced from filter.watch_config() (env defaults, overridden by the operator
    panel's .state/watch_config.json). `present_cats` is the set of feed
    categories that actually have briefs, so a watched type renders as live (✓)
    vs. watched-but-quiet (·) instead of falsely implying content that isn't
    there. Read-only here: the feed is a static artifact; only the panel writes.
    """
    cfg = watch_config()
    present = {c.lower() for c in (present_cats or set())}
    min_usd = cfg.get("min_usd") or 0.0
    bar = f"${min_usd:,.0f}"

    def pill(label: str, category: str, on: bool) -> str:
        # off = operator disabled it; on+present = has briefs in this feed (✓);
        # on+absent = watched but no qualifying event in this window (muted ·).
        if not on:
            return f"<span class='watch-pill off'>{html.escape(label)} <b>✗</b></span>"
        if category in present:
            return f"<span class='watch-pill on'>{html.escape(label)} <b>✓</b></span>"
        return (
            f"<span class='watch-pill watching' title='Watched — no qualifying "
            f"event in this feed yet'>{html.escape(label)} <b>·</b></span>"
        )

    nature = (
        pill("Deploys", "deployment", cfg.get("deploys", True))
        + pill("Governance", "governance", cfg.get("governance", True))
        + pill("Security", "security", cfg.get("security", True))
    )
    return (
        "<div class='watch-badge' title='This agent&#39;s watch policy (set via "
        "env vars or the local operator panel). A check marks a category with "
        "briefs in this feed; a dot marks a type that is watched but had no "
        "qualifying event in this window.'>"
        "<span class='watch-badge-label'>AGENT WATCH SETTINGS</span>"
        f"<span class='watch-bar'>Swaps &amp; transfers ≥ <b>{bar}</b></span>"
        f"<span class='watch-natures'>{nature}</span>"
        "</div>"
    )


# The five stages every published brief travels, from the real-world trigger to
# the MATCH a reader can reproduce in their own browser. Static and the same for
# the whole feed (each individual card carries its own per-brief links), so this
# is the 5-second "why should I trust this?" answer shown above the cards.
_LADDER_RUNGS = (
    ("1", "Trigger", "Real Solana tx"),
    ("2", "ACE · x402", "3 AI calls paid on Base"),
    ("3", "Memo", "Hashed to Solana mainnet"),
    ("4", "SAP", "Registered agent identity"),
    ("5", "MATCH", "Re-check in your browser"),
)


def _proof_ladder_html() -> str:
    """Compact end-to-end provenance flow strip, rendered above the feed.

    Anchors only (no inline handlers) so the page stays CSP-clean: the SAP rung
    opens the on-chain agent, the MATCH rung jumps to the cards (each of which
    carries its own Verify button), and the caption links the machine-readable
    proof.json. Rungs 1-3 are concepts the per-card links and proof.json prove.
    """
    parts: list[str] = []
    last = len(_LADDER_RUNGS) - 1
    for idx, (num, title, sub) in enumerate(_LADDER_RUNGS):
        inner = (
            f"<span class='rung-num'>{num}</span>"
            f"<span class='rung-title'>{html.escape(title)}</span>"
            f"<span class='rung-sub'>{html.escape(sub)}</span>"
        )
        if title == "SAP":
            parts.append(
                f"<a class='rung' href='{_AGENT_EXPLORER}' "
                f"target='_blank' rel='noopener noreferrer'>{inner}</a>"
            )
        elif title == "MATCH":
            parts.append(f"<a class='rung match' href='#feed'>{inner}</a>")
        else:
            parts.append(f"<div class='rung'>{inner}</div>")
        if idx != last:
            parts.append("<div class='ladder-arrow' aria-hidden='true'>&rarr;</div>")
    return (
        "<section class='proof-ladder'>"
        "<div class='proof-ladder-label'>How every brief is proven — end to end</div>"
        f"<div class='ladder-row'>{''.join(parts)}</div>"
        "<div class='proof-ladder-links'>"
        "<a href='#feed'>&darr; Verify any card</a>"
        "<a href='/proof.json' target='_blank' rel='noopener noreferrer'>Full proof.json &nearr;</a>"
        f"<a href='{_AGENT_EXPLORER}' target='_blank' rel='noopener noreferrer'>Agent on-chain &nearr;</a>"
        "</div>"
        "</section>"
    )


def _render(items: list[FeedItem], agent_payment_wallet: str = "") -> str:
    cards = []
    for it in items:
        # Generate sources list as domain badges
        sources_html = ""
        if it.sources:
            badges = []
            for s in it.sources:
                parsed = urlparse(s)
                label = parsed.netloc.replace("www.", "")
                # Solscan account/tx links would all render as a bare "solscan.io"
                # pill; show the entity stub instead so a program account vs its
                # upgrade authority vs the tx stay distinguishable.
                if label == "solscan.io":
                    parts = [p for p in parsed.path.split("/") if p]
                    if len(parts) >= 2 and parts[0] in ("account", "tx"):
                        ident = parts[1]
                        stub = f"{ident[:4]}…{ident[-4:]}" if len(ident) > 9 else ident
                        label = f"{parts[0]} {stub}"
                if not label:
                    label = "source"
                badges.append(
                    f'<a class="source-badge" href="{html.escape(s)}" target="_blank" rel="noopener noreferrer">{html.escape(label)}</a>'
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
            f'<span>{sig[:6]}…{sig[-6:]}</span>'
            f'</a>'
        )

        # Attestation signature / Verify on-chain button
        attest_html = ""
        if it.attest_sig:
            asig = html.escape(it.attest_sig)
            acl = html.escape(it.attest_cluster)
            attest_html = (
                f'<button class="proof-badge attest verify" data-action="verify" '
                f'data-sig="{sig}" data-asig="{asig}" data-cluster="{acl}" '
                f'data-card="{html.escape(it.card)}" data-md="{html.escape(it.md_name)}" '
                f'style="cursor:pointer; width:100%; text-align:left; display:flex; justify-content:space-between; align-items:center;">'
                f'<span class="badge-label">🛡️ Verify On-Chain ({acl})</span>'
                f'<span>{asig[:6]}…{asig[-6:]}</span>'
                f'</button>'
            )

        img_html = ""
        if it.card:
            img_html = (
                f'<div class="img-wrapper">'
                f'<img src="{html.escape(it.card)}" alt="{html.escape(it.headline)}" data-action="lightbox" style="cursor:zoom-in;">'
                f'</div>'
            )

        cat = html.escape(it.category)
        # Only value-moves carry a data-usd; nature events omit it so the amount
        # slider never hides a deploy/governance/security brief.
        usd_attr = f' data-usd="{it.amount_usd:.2f}"' if it.amount_usd > 0 else ""

        cards.append(
            f'<article data-category="{cat.lower()}"{usd_attr}>'
            f'{img_html}'
            f'<h2>{html.escape(_display_headline(it.headline))}</h2>'
            f'<p class="narrative">{html.escape(it.narrative)}</p>'
            f'{sources_html}'
            f'<div class="proofs">'
            f'{provenance_html}'
            f'{attest_html}'
            f'</div>'
            f'</article>'
        )

    body = "\n".join(cards) if cards else '<div style="grid-column: 1/-1; text-align: center; color: var(--text-muted); padding: 40px 0;">No briefs yet</div>'
    request_section = ""
    pay_sol = lamports_to_sol_str(REQUEST_BRIEF_LAMPORTS)
    pay_cluster_label = "Devnet" if PAYMENT_CLUSTER == "devnet" else "Mainnet"
    if agent_payment_wallet:
        request_section = (
            "<section class='request-brief-section'>"
            "<div class='request-card'>"
            "<h3>Request an On-Demand Brief</h3>"
            f"<p>Submit a Solana transaction signature. The payment ({pay_sol} {pay_cluster_label} SOL) carries a Memo binding it to that request, and the server rejects reused payments.</p>"
            "<div class='request-form'>"
            "<input type='text' id='tx-sig-input' placeholder='Enter Solana Transaction Signature' />"
            "<button id='connect-wallet-btn' data-action='connect-wallet'>Connect Phantom</button>"
            f"<button id='submit-request-btn' data-action='submit-request' disabled>Request Brief ({pay_sol} {pay_cluster_label} SOL)</button>"
            "</div>"
            "<div id='wallet-status' class='wallet-status-idle'>Wallet not connected</div>"
            "<div id='request-status-msg' style='margin-top: 12px; font-size: 12px; font-family: monospace;'></div>"
            "</div>"
            "</section>"
        )

    # Only render category pills that actually have briefs — otherwise an empty
    # tab (e.g. Security with zero briefs) shows a blank void. "All" is always
    # present. The page is re-rendered at boot, so pills always match the cards.
    present_cats = {it.category.lower() for it in items}
    _cat_pills = [
        ("security", "Security"),
        ("deployment", "Deployments"),
        ("volume", "Volume"),
        ("governance", "Governance"),
        ("activity", "Activity"),
    ]
    filter_buttons = (
        "<button class='filter-btn active' data-action='filter' data-category='all'>All</button>"
    )
    for _slug, _label in _cat_pills:
        if _slug in present_cats:
            filter_buttons += (
                f"<button class='filter-btn' data-action='filter' data-category='{_slug}'>{_label}</button>"
            )

    # Client-side amount filter. Only shown when there is at least one value-move
    # to filter by; the slider's ceiling is the largest brief on the feed, rounded
    # up so the top card stays reachable at max. Nature events ignore it.
    usd_values = [it.amount_usd for it in items if it.amount_usd > 0]
    amount_slider = ""
    if usd_values:
        slider_max = int(math.ceil(max(usd_values)))
        amount_slider = (
            "<div class='usd-filter'>"
            "<label for='usd-slider'>Min amount</label>"
            f"<input type='range' id='usd-slider' min='0' max='{slider_max}' "
            "value='0' step='1' data-action='usd-filter'>"
            "<span id='usd-slider-val'>$0</span>"
            "</div>"
        )

    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OnchainBrief — Verified Solana Intelligence</title>"
        "<script src='https://unpkg.com/@solana/web3.js@1.98.4/lib/index.iife.min.js' "
        "integrity='sha384-I45YF+S0YGWIolUyTksLk9TNtTqaDgZg8e6T1OoBoJvvFmphqYNIPZw3Kl0TkZNN' "
        "crossorigin='anonymous'></script>"
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
        "aspect-ratio:1.75;"
        "background:#1f2937;"
        "margin-bottom:20px;"
        "border:1px solid rgba(255,255,255,0.05)"
        "}"
        "article img{"
        "width:100%;"
        "height:100%;"
        "object-fit:cover;"
        "object-position:center;"
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
        "font-family:monospace;"
        "background:none;"
        "border:none;"
        "color:inherit"
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
        ".lightbox{"
        "display:none;"
        "position:fixed;"
        "z-index:1000;"
        "left:0;"
        "top:0;"
        "width:100%;"
        "height:100%;"
        "background:rgba(3,7,18,0.95);"
        "backdrop-filter:blur(10px);"
        "align-items:center;"
        "justify-content:center;"
        "flex-direction:column;"
        "cursor:zoom-out"
        "}"
        ".lightbox-content{"
        "max-width:90%;"
        "max-height:80%;"
        "border-radius:16px;"
        "border:2px solid rgba(139,92,246,0.3);"
        "box-shadow:0 0 40px rgba(139,92,246,0.4);"
        "animation:zoom 0.25s cubic-bezier(0.4,0,0.2,1)"
        "}"
        "@keyframes zoom{from{transform:scale(0.95);opacity:0}to{transform:scale(1);opacity:1}}"
        ".lightbox-close{"
        "position:absolute;"
        "top:20px;"
        "right:35px;"
        "color:#9ca3af;"
        "font-size:40px;"
        "font-weight:300;"
        "cursor:pointer;"
        "transition:color 0.2s"
        "}"
        ".lightbox-close:hover{color:#ffffff}"
        ".lightbox-caption{"
        "color:#9ca3af;"
        "margin-top:20px;"
        "font-size:14px;"
        "font-family:monospace;"
        "text-align:center"
        "}"
        "nav{display:flex;align-items:center;justify-content:space-between;padding:20px 0;margin-bottom:48px;border-bottom:1px solid var(--border)}"
        ".nav-logo{display:flex;align-items:center;gap:10px;text-decoration:none}"
        ".nav-logo-icon{font-size:22px;background:linear-gradient(135deg,var(--primary),var(--accent));-webkit-background-clip:text;-webkit-text-fill-color:transparent;filter:drop-shadow(0 2px 8px var(--primary-glow))}"
        ".nav-logo-text{font-size:20px;font-weight:700;color:#ffffff;letter-spacing:-0.02em}"
        ".nav-status{display:flex;align-items:center;gap:8px;font-size:11px;color:#34d399;font-family:monospace;background:rgba(16,185,129,0.06);border:1px solid rgba(16,185,129,0.15);padding:4px 10px;border-radius:999px;font-weight:600}"
        ".nav-status-dot{width:6px;height:6px;border-radius:50%;background:#10b981;box-shadow:0 0 8px #10b981;animation:pulse 2s infinite}"
        "@keyframes pulse{0%{opacity:0.4}50%{opacity:1}100%{opacity:0.4}}"
        ".hero{text-align:center;margin-bottom:48px;max-width:800px;margin-left:auto;margin-right:auto}"
        ".hero-badge{display:inline-flex;align-items:center;background:var(--primary-glow);border:1px solid rgba(139,92,246,0.3);color:#c084fc;padding:6px 14px;border-radius:999px;font-size:11px;font-weight:600;letter-spacing:0.05em;margin-bottom:20px}"
        ".hero h1{font-size:40px;font-weight:700;margin:0 0 16px 0;background:linear-gradient(to right,#ffffff,#d1d5db);-webkit-background-clip:text;-webkit-text-fill-color:transparent;letter-spacing:-0.02em;line-height:1.2}"
        ".hero-desc{color:var(--text-muted);font-size:16px;line-height:1.6;margin:0;font-weight:300}"
        ".value-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-bottom:48px}"
        ".value-card{background:linear-gradient(135deg,rgba(17,24,39,0.5),rgba(31,41,55,0.2));border:1px solid var(--border);border-radius:20px;padding:24px;display:flex;flex-direction:column;gap:12px;transition:all 0.3s ease}"
        ".value-card:hover{border-color:rgba(139,92,246,0.2);box-shadow:0 12px 30px rgba(0,0,0,0.4),0 0 1px var(--primary);transform:translateY(-2px)}"
        ".value-question{font-size:10px;font-weight:700;text-transform:uppercase;color:var(--accent);letter-spacing:0.05em}"
        ".value-card.v2 .value-question{color:var(--primary)}"
        ".value-card.v3 .value-question{color:var(--success)}"
        ".value-card h3{font-size:16px;font-weight:600;color:#ffffff;margin:0}"
        ".value-card p{font-size:13px;line-height:1.6;color:var(--text-muted);margin:0;font-weight:300}"

        "/* Proof-ladder — end-to-end provenance flow strip */"
        ".proof-ladder{max-width:880px;margin:0 auto 48px auto;padding:22px 26px;background:linear-gradient(135deg,rgba(139,92,246,0.06),rgba(6,182,212,0.04));border:1px solid rgba(139,92,246,0.18);border-radius:20px}"
        ".proof-ladder-label{text-align:center;font-size:10px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;color:var(--accent);margin-bottom:18px}"
        ".ladder-row{display:flex;align-items:flex-start;justify-content:center}"
        ".rung{flex:1 1 0;min-width:0;display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px;padding:0 4px;text-decoration:none;color:inherit}"
        ".rung-num{width:30px;height:30px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;font-weight:700;font-family:monospace;background:rgba(139,92,246,0.12);border:1px solid rgba(139,92,246,0.35);color:#c084fc;transition:box-shadow 0.2s ease}"
        ".rung.match .rung-num{background:var(--success-glow);border-color:rgba(16,185,129,0.45);color:#34d399}"
        ".rung-title{font-size:12px;font-weight:700;color:#fff;letter-spacing:0.01em}"
        ".rung-sub{font-size:10px;color:var(--text-muted);line-height:1.35;font-weight:300}"
        "a.rung:hover{transform:translateY(-2px)}"
        "a.rung:hover .rung-num{box-shadow:0 0 12px rgba(139,92,246,0.45)}"
        "a.rung.match:hover .rung-num{box-shadow:0 0 12px rgba(16,185,129,0.45)}"
        ".ladder-arrow{flex:0 0 auto;color:var(--text-muted);font-size:18px;padding-top:6px;opacity:0.5}"
        ".proof-ladder-links{display:flex;justify-content:center;gap:20px;flex-wrap:wrap;margin-top:18px;padding-top:16px;border-top:1px solid rgba(255,255,255,0.06)}"
        ".proof-ladder-links a{font-size:11px;color:var(--accent);text-decoration:none;font-weight:600}"
        ".proof-ladder-links a:hover{text-decoration:underline}"
        "@media (max-width:640px){.ladder-row{flex-direction:column;align-items:stretch;gap:2px}.rung{flex-direction:row;justify-content:flex-start;text-align:left;gap:12px;padding:6px 0}.ladder-arrow{transform:rotate(90deg);padding:0;margin-left:14px}}"

        "/* New Overhaul CSS styles */"
        ".watch-badge{display:flex;justify-content:center;align-items:center;gap:14px;flex-wrap:wrap;max-width:760px;margin:0 auto 20px auto;padding:10px 18px;background:rgba(255,255,255,0.02);border:1px solid rgba(255,255,255,0.06);border-radius:14px;font-size:12px;color:var(--text-muted)}"
        ".watch-badge-label{font-size:10px;font-weight:700;letter-spacing:0.06em;color:var(--accent);text-transform:uppercase}"
        ".watch-bar{font-weight:300}"
        ".watch-bar b{color:#ffffff;font-weight:600}"
        ".watch-natures{display:flex;gap:8px;flex-wrap:wrap}"
        ".watch-pill{display:inline-flex;align-items:center;gap:4px;padding:3px 10px;border-radius:999px;font-size:11px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06)}"
        ".watch-pill.on b{color:var(--success)}"
        ".watch-pill.off{opacity:0.45}.watch-pill.off b{color:#f87171}"
        ".watch-pill.watching{opacity:0.55}.watch-pill.watching b{color:var(--text-muted);font-weight:700}"
        ".filter-bar{display:flex;justify-content:center;gap:10px;margin-bottom:40px;flex-wrap:wrap}"
        ".filter-btn{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);color:var(--text-muted);padding:8px 16px;border-radius:999px;font-size:12px;font-weight:600;cursor:pointer;transition:all 0.2s cubic-bezier(0.4,0,0.2,1);text-transform:uppercase;letter-spacing:0.04em}"
        ".filter-btn:hover{background:rgba(255,255,255,0.08);border-color:rgba(255,255,255,0.15);color:#ffffff}"
        ".filter-btn.active{background:var(--primary);border-color:var(--primary);color:#ffffff;box-shadow:0 0 15px rgba(139,92,246,0.4)}"
        ".usd-filter{display:flex;align-items:center;justify-content:center;gap:12px;max-width:520px;margin:0 auto 40px auto;font-size:12px;color:var(--text-muted)}"
        ".usd-filter label{font-size:10px;font-weight:700;letter-spacing:0.06em;text-transform:uppercase;white-space:nowrap}"
        ".usd-filter input[type=range]{flex:1;accent-color:var(--primary);cursor:pointer;height:4px}"
        "#usd-slider-val{font-family:monospace;color:#ffffff;min-width:90px;text-align:right;font-weight:600}"

        ".request-brief-section{max-width:800px;margin:0 auto 48px auto}"
        ".request-card{background:linear-gradient(135deg,rgba(255,255,255,0.03),rgba(255,255,255,0.01));backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid var(--border);border-radius:20px;padding:24px;text-align:center;box-shadow:0 8px 32px rgba(0,0,0,0.4);transition:all 0.3s ease}"
        ".request-card:hover{border-color:rgba(139,92,246,0.25);box-shadow:0 12px 40px rgba(139,92,246,0.1)}"
        ".request-card h3{margin-top:0;font-size:17px;font-weight:600;color:#ffffff;margin-bottom:6px}"
        ".request-card p{font-size:13px;color:var(--text-muted);margin-bottom:18px;line-height:1.5;font-weight:300}"
        ".request-form{display:flex;gap:10px;justify-content:center;align-items:center;flex-wrap:wrap}"
        "#tx-sig-input{flex-grow:1;min-width:280px;background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.08);border-radius:10px;padding:12px 14px;color:#ffffff;font-family:monospace;font-size:13px;transition:all 0.3s ease}"
        "#tx-sig-input:focus{outline:none;border-color:var(--primary);box-shadow:0 0 8px rgba(139,92,246,0.2)}"
        "#connect-wallet-btn,#submit-request-btn{background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.08);color:#ffffff;padding:12px 18px;border-radius:10px;font-size:12px;font-weight:700;cursor:pointer;transition:all 0.3s ease;text-transform:uppercase;letter-spacing:0.04em}"
        "#connect-wallet-btn:hover{background:rgba(255,255,255,0.08)}"
        "#submit-request-btn{background:var(--primary);border-color:var(--primary)}"
        "#submit-request-btn:hover:not(:disabled){background:#7c3aed;box-shadow:0 0 15px rgba(139,92,246,0.4)}"
        "#submit-request-btn:disabled{opacity:0.4;cursor:not-allowed;background:rgba(255,255,255,0.01);border-color:rgba(255,255,255,0.03);color:var(--text-muted)}"
        ".wallet-status-idle{font-size:11px;color:var(--text-muted);margin-top:10px;font-family:monospace}"
        ".wallet-status-connected{font-size:11px;color:var(--success);margin-top:10px;font-family:monospace}"
        
        ".verify-modal-content{background:rgba(17,24,39,0.96);backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);border:1px solid rgba(139,92,246,0.25);border-radius:20px;padding:32px;width:90%;max-width:680px;box-shadow:0 20px 50px rgba(0,0,0,0.8),0 0 1px var(--primary);animation:zoom 0.3s cubic-bezier(0.4,0,0.2,1);color:var(--text);text-align:left;position:relative}"
        ".audit-section{margin-bottom:22px;border-bottom:1px solid rgba(255,255,255,0.05);padding-bottom:18px}"
        ".audit-section:last-child{border-bottom:none;padding-bottom:0;margin-bottom:0}"
        ".audit-section h3{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;color:var(--text-muted);margin:0 0 12px 0}"
        ".audit-timeline{display:flex;flex-direction:column;gap:14px}"
        ".timeline-step{display:flex;gap:14px;align-items:flex-start}"
        ".timeline-dot{width:8px;height:8px;border-radius:50%;margin-top:5px;flex-shrink:0}"
        ".timeline-dot.success{background:var(--success);box-shadow:0 0 8px var(--success)}"
        ".timeline-dot.loading{background:var(--accent);box-shadow:0 0 8px var(--accent);animation:pulse-dot 1.5s infinite}"
        "@keyframes pulse-dot{0%{opacity:0.4}50%{opacity:1}100%{opacity:0.4}}"
        ".timeline-info{display:flex;flex-direction:column}"
        ".timeline-title{font-size:13px;font-weight:600;color:#ffffff}"
        ".timeline-detail{font-size:11px;color:var(--text-muted);font-family:monospace;word-break:break-all}"
        ".timeline-detail a{color:var(--accent);text-decoration:none}"
        ".timeline-detail a:hover{text-decoration:underline}"
        ".audit-table{width:100%;border-collapse:collapse}"
        ".audit-table th,.audit-table td{padding:8px 10px;text-align:left;font-size:12px;border-bottom:1px solid rgba(255,255,255,0.03)}"
        ".audit-table th{color:var(--text-muted);font-weight:500;width:35%}"
        ".audit-table td.mono{font-family:monospace;word-break:break-all}"
        ".audit-payload{background:rgba(0,0,0,0.4);border:1px solid rgba(255,255,255,0.05);border-radius:8px;padding:12px;font-family:monospace;font-size:11px;color:#34d399;overflow-x:auto;margin:0;max-height:180px}"
        ".status-match{color:var(--success);font-weight:600}"
        ".status-mismatch{color:#f87171;font-weight:600}"
        
        ".toast-container{position:fixed;top:24px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:10px;pointer-events:none}"
        ".toast{pointer-events:auto;background:rgba(17,24,39,0.95);backdrop-filter:blur(16px);-webkit-backdrop-filter:blur(16px);border:1px solid rgba(255,255,255,0.1);border-radius:14px;padding:14px 20px;color:#f3f4f6;font-size:13px;font-family:'Inter',sans-serif;display:flex;align-items:center;gap:12px;box-shadow:0 8px 32px rgba(0,0,0,0.5);transform:translateX(120%);animation:toast-in 0.4s cubic-bezier(0.16,1,0.3,1) forwards;max-width:420px;line-height:1.4}"
        ".toast.toast-out{animation:toast-out 0.35s cubic-bezier(0.4,0,1,1) forwards}"
        ".toast-icon{font-size:18px;flex-shrink:0}"
        ".toast.error{border-color:rgba(239,68,68,0.3)}"
        ".toast.warning{border-color:rgba(251,191,36,0.3)}"
        ".toast.info{border-color:rgba(139,92,246,0.3)}"
        ".toast.success{border-color:rgba(16,185,129,0.3)}"
        "@keyframes toast-in{from{transform:translateX(120%);opacity:0}to{transform:translateX(0);opacity:1}}"
        "@keyframes toast-out{from{transform:translateX(0);opacity:1}to{transform:translateX(120%);opacity:0}}"
        
        "@media (max-width:768px){"
        ".value-grid{grid-template-columns:1fr;gap:20px}"
        ".hero h1{font-size:30px}"
        "nav{flex-direction:column;gap:12px;align-items:flex-start}"
        ".toast-container{left:16px;right:16px}"
        ".toast{max-width:100%}"
        "}"
        "</style></head><body>"
        "<div id='toast-container' class='toast-container'></div>"
        "<div class=container>"
        "<nav>"
        "<a href='#' class=nav-logo>"
        "<span class=nav-logo-icon>⚡</span>"
        "<span class=nav-logo-text>OnchainBrief</span>"
        "</a>"
        "<div class=nav-status>"
        "<span class=nav-status-dot></span>"
        "<span>AGENT ACTIVE (SOLANA MAINNET)</span>"
        "</div>"
        "</nav>"
        "<section class=hero>"
        "<div class=hero-badge>⚡ ON-CHAIN EVENT BRIEFS</div>"
        "<h1>Autonomous Solana Whale & Event Intelligence</h1>"
        "<p class=hero-desc>OnchainBrief reads Solana mainnet as blocks land and turns consequential transactions — large transfers, new program deployments, sizable swaps, and governance actions — into short, sourced briefs. Each one is hashed onto the ledger itself, so its origin can be checked independently.</p>"
        "</section>"

        # Proof-ladder: the end-to-end "how is this proven?" flow, shown up front.
        f"{_proof_ladder_html()}"
        
        f"{request_section}"
        
        "<section class=value-grid>"
        "<div class='value-card v1'>"
        "<div class=value-question>WHAT IS ONCHAINBRIEF?</div>"
        "<h3>Real-time Event Watcher</h3>"
        "<p>An autonomous agent follows the Solana ledger block by block and isolates the events worth reading — whale transfers, new program deployments, large swaps, and governance actions — from the steady stream of routine activity.</p>"
        "</div>"
        "<div class='value-card v2'>"
        "<div class=value-question>WHY DOES IT EXIST?</div>"
        "<h3>Raw Logs, Made Readable</h3>"
        "<p>Transaction logs are dense and hard to parse. The agent decodes what actually moved — amount, asset, sender and recipient — adds sourced context on the entities involved, and writes a two-line brief: what happened, and why it matters.</p>"
        "</div>"
        "<div class='value-card v3'>"
        "<div class=value-question>WHAT VALUE DOES IT BRING?</div>"
        "<h3>Verifiable Ledger Provenance</h3>"
        "<p>Every brief is hashed and recorded on Solana's ledger through the Memo and SAP protocols. The record is permanent and cannot be changed after the fact, so any reader can confirm that a brief existed — unchanged — at the moment it was published.</p>"
        "</div>"
        "</section>"
        
        # Read-only banner: the agent's live editorial bar (operator-configurable)
        f"{_watch_badge_html(present_cats)}"

        # Filter tabs
        "<div class='filter-bar'>"
        f"{filter_buttons}"
        "</div>"
        # Client-side amount slider (only present when there are value-moves)
        f"{amount_slider}"

        f"<main class=grid id=feed>{body}</main>"
        "</div>"
        "<footer>"
        "<p>Powered by ACE Services & Solana. Attested via Agent PDA. <a href='https://github.com/cryptoyasenka/onchainbrief' target=_blank rel='noopener noreferrer'>View Source</a></p>"
        "</footer>"
        
        # Verify modal HTML
        "<div id='verify-modal' class='lightbox' data-action='close-verify' style='display:none; cursor:default;'>"
        "<div class='verify-modal-content' data-action='noop'>"
        "<span class='lightbox-close' data-action='close-verify'>&times;</span>"
        "<h2>🛡️ On-Chain Provenance Audit</h2>"
        "<div class='audit-section'>"
        "<div class='audit-timeline'>"
        "<div class='timeline-step'>"
        "<div class='timeline-dot success'></div>"
        "<div class='timeline-info'>"
        "<div class='timeline-title'>Event Signature</div>"
        "<div class='timeline-detail' id='audit-event-sig'>-</div>"
        "</div>"
        "</div>"
        "<div class='timeline-step'>"
        "<div class='timeline-dot success'></div>"
        "<div class='timeline-info'>"
        "<div class='timeline-title'>Agent Analysis</div>"
        "<div class='timeline-detail'>Brief generated and hashed successfully</div>"
        "</div>"
        "</div>"
        "<div class='timeline-step'>"
        "<div class='timeline-dot success' id='audit-attest-dot'></div>"
        "<div class='timeline-info'>"
        "<div class='timeline-title'>Attestation Ledger Entry</div>"
        "<div class='timeline-detail' id='audit-attest-sig'>-</div>"
        "</div>"
        "</div>"
        "</div>"
        "</div>"
        "<div class='audit-section'>"
        "<h3>Cryptographic Integrity Check</h3>"
        "<table class='audit-table'>"
        "<tr>"
        "<th>Local Artifacts Hash (SHA-256)</th>"
        "<td class='mono' id='audit-local-hash'>Calculating...</td>"
        "</tr>"
        "<tr>"
        "<th>On-Chain Registered Hash</th>"
        "<td class='mono' id='audit-onchain-hash'>Fetching...</td>"
        "</tr>"
        "<tr>"
        "<th>Verification Status</th>"
        "<td id='audit-status'>Verifying...</td>"
        "</tr>"
        "</table>"
        "</div>"
        "<div class='audit-section' id='audit-payload-section' style='display:none;'>"
        "<h3>Attestation Payload (Decoded JSON)</h3>"
        "<pre class='audit-payload' id='audit-payload-text'></pre>"
        "</div>"
        "</div>"
        "</div>"
        
        "<div id=lightbox class=lightbox data-action='close-lightbox'>"
        "<span class=lightbox-close>&times;</span>"
        "<img class=lightbox-content id=lightbox-img>"
        "<div class=lightbox-caption id=lightbox-caption></div>"
        "</div>"
        "<script>"
        f"const AGENT_PAYMENT_WALLET = '{html.escape(agent_payment_wallet)}';"
        "const MEMO_PROGRAM_ID = 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr';"
        "let userWallet = null;"
        
        "function showToast(message, type) {"
        "  type = type || 'info';"
        "  var icons = {error:'❌',warning:'⚠️',info:'ℹ️',success:'✅'};"
        "  var container = document.getElementById('toast-container');"
        "  var toast = document.createElement('div');"
        "  toast.className = 'toast ' + type;"
        "  var iconSpan = document.createElement('span');"
        "  iconSpan.className = 'toast-icon';"
        "  iconSpan.textContent = icons[type] || 'ℹ️';"
        "  var msgSpan = document.createElement('span');"
        "  msgSpan.textContent = message;"
        "  toast.appendChild(iconSpan);"
        "  toast.appendChild(msgSpan);"
        "  container.appendChild(toast);"
        "  setTimeout(function() {"
        "    toast.classList.add('toast-out');"
        "    toast.addEventListener('animationend', function() { toast.remove(); });"
        "  }, 4000);"
        "}"
        
        # Base58 decoder
        "const B58_ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz';"
        "function decodeBase58(str) {"
        "  let bytes = [0];"
        "  for (let i = 0; i < str.length; i++) {"
        "    let charIndex = B58_ALPHABET.indexOf(str[i]);"
        "    if (charIndex === -1) return null;"
        "    let carry = charIndex;"
        "    for (let j = 0; j < bytes.length; j++) {"
        "      let val = bytes[j] * 58 + carry;"
        "      bytes[j] = val % 256;"
        "      carry = Math.floor(val / 256);"
        "    }"
        "    while (carry > 0) {"
        "      bytes.push(carry % 256);"
        "      carry = Math.floor(carry / 256);"
        "    }"
        "  }"
        "  for (let i = 0; i < str.length && str[i] === '1'; i++) {"
        "    bytes.push(0);"
        "  }"
        "  return new TextDecoder().decode(new Uint8Array(bytes.reverse()));"
        "}"
        
        "function openLightbox(src,alt){"
        "document.getElementById('lightbox').style.display='flex';"
        "document.getElementById('lightbox-img').src=src;"
        "document.getElementById('lightbox-caption').textContent=alt;"
        "}"
        "function closeLightbox(){"
        "document.getElementById('lightbox').style.display='none';"
        "}"
        
        "function openVerifyModal() {"
        "  document.getElementById('verify-modal').style.display = 'flex';"
        "}"
        "function closeVerifyModal() {"
        "  document.getElementById('verify-modal').style.display = 'none';"
        "}"
        
        "async function verifyBrief(txSig, attestSig, cluster, cardImgName, mdName) {"
        "  openVerifyModal();"
        "  const eventSigEl = document.getElementById('audit-event-sig');"
        "  eventSigEl.textContent = '';"
        "  const eventLink = document.createElement('a');"
        "  eventLink.href = 'https://solscan.io/tx/' + txSig;"
        "  eventLink.target = '_blank';"
        "  eventLink.rel = 'noopener noreferrer';"
        "  eventLink.textContent = txSig;"
        "  eventSigEl.appendChild(eventLink);"
        "  const attestSigEl = document.getElementById('audit-attest-sig');"
        "  attestSigEl.textContent = '';"
        "  const attestLink = document.createElement('a');"
        "  attestLink.href = 'https://solscan.io/tx/' + attestSig + '?cluster=' + cluster;"
        "  attestLink.target = '_blank';"
        "  attestLink.rel = 'noopener noreferrer';"
        "  attestLink.textContent = attestSig;"
        "  attestSigEl.appendChild(attestLink);"
        "  document.getElementById('audit-local-hash').textContent = 'Calculating...';"
        "  document.getElementById('audit-onchain-hash').textContent = 'Fetching from ledger...';"
        "  const statusEl = document.getElementById('audit-status');"
        "  statusEl.className = '';"
        "  statusEl.textContent = 'Verifying...';"
        "  const payloadSec = document.getElementById('audit-payload-section');"
        "  payloadSec.style.display = 'none';"
        "  const attestDot = document.getElementById('audit-attest-dot');"
        "  attestDot.className = 'timeline-dot loading';"
        
        "  try {"
        f"    const rpcUrl = cluster === 'mainnet-beta' ? '{MAINNET_RPC_URL}' : '{DEVNET_RPC_URL}';"
        "    const response = await fetch(rpcUrl, {"
        "      method: 'POST',"
        "      headers: { 'Content-Type': 'application/json' },"
        "      body: JSON.stringify({"
        "        jsonrpc: '2.0',"
        "        id: 1,"
        "        method: 'getTransaction',"
        "        params: [attestSig, { encoding: 'jsonParsed', maxSupportedTransactionVersion: 0 }]"
        "      })"
        "    });"
        "    if (!response.ok) throw new Error(`RPC status ${response.status}`);"
        "    const data = await response.json();"
        "    if (data.error) throw new Error(data.error.message || 'RPC Error');"
        "    const tx = data.result;"
        "    if (!tx) throw new Error('Transaction not found on-chain');"
        "    attestDot.className = 'timeline-dot success';"
        
        "    let memoText = null;"
        "    if (tx.transaction && tx.transaction.message) {"
        "      for (const ix of tx.transaction.message.instructions) {"
        "        if (ix.programId === 'MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr') {"
        "          if (typeof ix.parsed === 'string') {"
        "            memoText = ix.parsed;"
        "          } else if (ix.data) {"
        "            memoText = decodeBase58(ix.data);"
        "          }"
        "        }"
        "      }"
        "    }"
        "    if (!memoText && tx.meta && tx.meta.logMessages) {"
        "      for (const log of tx.meta.logMessages) {"
        "        if (log.includes('Program log: Memo (v2):')) {"
        "          memoText = log.split('Program log: Memo (v2):')[1].trim();"
        "          break;"
        "        }"
        "      }"
        "    }"
        "    if (!memoText) throw new Error('SPL Memo payload not found in transaction');"
        
        "    let payload;"
        "    try {"
        "      payload = JSON.parse(memoText);"
        "    } catch (e) {"
        "      const cleaned = memoText.substring(memoText.indexOf('{'), memoText.lastIndexOf('}') + 1);"
        "      payload = JSON.parse(cleaned);"
        "    }"
        
        "    const onchainHash = payload.sha256;"
        "    document.getElementById('audit-onchain-hash').textContent = onchainHash;"
        "    payloadSec.style.display = 'block';"
        "    document.getElementById('audit-payload-text').textContent = JSON.stringify(payload, null, 2);"
        
        "    const mdRes = await fetch(mdName);"
        "    if (!mdRes.ok) throw new Error(`Failed to fetch brief markdown file ${mdName}`);"
        "    const mdBuf = await mdRes.arrayBuffer();"
        "    let finalBuf;"
        "    if (cardImgName && cardImgName !== 'None') {"
        "      const pngRes = await fetch(cardImgName);"
        "      if (!pngRes.ok) throw new Error(`Failed to fetch card image file ${cardImgName}`);"
        "      const pngBuf = await pngRes.arrayBuffer();"
        "      finalBuf = new Uint8Array(pngBuf.byteLength + mdBuf.byteLength);"
        "      finalBuf.set(new Uint8Array(pngBuf), 0);"
        "      finalBuf.set(new Uint8Array(mdBuf), pngBuf.byteLength);"
        "    } else {"
        "      finalBuf = mdBuf;"
        "    }"
        
        "    const hashBuffer = await crypto.subtle.digest('SHA-256', finalBuf);"
        "    const hashArray = Array.from(new Uint8Array(hashBuffer));"
        "    const localHash = hashArray.map(b => b.toString(16).padStart(2, '0')).join('');"
        "    document.getElementById('audit-local-hash').textContent = localHash;"
        
        "    if (localHash === onchainHash) {"
        "      statusEl.className = 'status-match';"
        "      statusEl.textContent = '✅ MATCH (Artifacts are identical to attested originals)';"
        "    } else {"
        "      statusEl.className = 'status-mismatch';"
        "      statusEl.textContent = '❌ MISMATCH (Artifacts have been modified)';"
        "    }"
        "  } catch (err) {"
        "    console.error(err);"
        "    statusEl.className = 'status-mismatch';"
        "    statusEl.textContent = 'Verification failed: ' + err.message;"
        "    attestDot.className = 'timeline-dot';"
        "  }"
        "}"
        
        # Category pills and the amount slider compose: a card shows only if it
        # clears BOTH. Cards without data-usd (nature events) ignore the slider.
        "window.__activeCat = 'all';"
        "window.__minUsd = 0;"
        "window.applyFeedFilters = function() {"
        "  const cat = window.__activeCat;"
        "  const minUsd = window.__minUsd;"
        "  document.querySelectorAll('.grid article').forEach(card => {"
        "    const cardCat = card.getAttribute('data-category');"
        "    const usdAttr = card.getAttribute('data-usd');"
        "    const catOk = (cat === 'all' || cardCat === cat);"
        "    const usdOk = (usdAttr === null || usdAttr === '') ? true : (parseFloat(usdAttr) >= minUsd);"
        "    if (catOk && usdOk) {"
        "      card.style.display = 'flex';"
        "      card.style.opacity = '0';"
        "      setTimeout(() => {"
        "        card.style.opacity = '1';"
        "        card.style.transition = 'opacity 0.3s ease';"
        "      }, 10);"
        "    } else {"
        "      card.style.display = 'none';"
        "    }"
        "  });"
        "};"
        "function filterCategory(cat, btnEl) {"
        "  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));"
        "  if (btnEl) btnEl.classList.add('active');"
        "  window.__activeCat = cat;"
        "  window.applyFeedFilters();"
        "}"
        "window.applyUsdFilter = function(val) {"
        "  const n = parseFloat(val) || 0;"
        "  window.__minUsd = n;"
        "  const lbl = document.getElementById('usd-slider-val');"
        "  if (lbl) lbl.textContent = '$' + n.toLocaleString('en-US');"
        "  window.applyFeedFilters();"
        "};"
        
        "async function toggleWalletConnect() {"
        "  if (!window.solana || !window.solana.isPhantom) {"
        "    showToast('Phantom Wallet not found. Please install it from phantom.app', 'warning');"
        "    return;"
        "  }"
        "  try {"
        "    if (userWallet) {"
        "      await window.solana.disconnect();"
        "      userWallet = null;"
        "      document.getElementById('connect-wallet-btn').textContent = 'Connect Phantom';"
        "      document.getElementById('submit-request-btn').disabled = true;"
        "      const status = document.getElementById('wallet-status');"
        "      status.textContent = 'Wallet disconnected';"
        "      status.className = 'wallet-status-idle';"
        "    } else {"
        "      const resp = await window.solana.connect();"
        "      userWallet = resp.publicKey.toString();"
        "      document.getElementById('connect-wallet-btn').textContent = 'Disconnect (' + userWallet.slice(0,4) + '...' + userWallet.slice(-4) + ')';"
        "      document.getElementById('submit-request-btn').disabled = false;"
        "      const status = document.getElementById('wallet-status');"
        "      status.textContent = 'Connected: ' + userWallet;"
        "      status.className = 'wallet-status-connected';"
        "    }"
        "  } catch (err) {"
        "    console.error(err);"
        "    showToast('Wallet connection failed: ' + err.message, 'error');"
        "  }"
        "}"
        
        "async function sendPaymentTx(senderPublicKeyStr, amountSol) {"
        "  if (!window.solanaWeb3) throw new Error('Solana Web3 library not loaded. Check connection.');"
        f"  const rpcUrl = '{PAYMENT_RPC_URL}';"
        "  const connection = new solanaWeb3.Connection(rpcUrl, 'confirmed');"
        "  const sender = new solanaWeb3.PublicKey(senderPublicKeyStr);"
        "  const recipient = new solanaWeb3.PublicKey(AGENT_PAYMENT_WALLET);"
        "  const { blockhash } = await connection.getLatestBlockhash();"
        "  const targetSig = document.getElementById('tx-sig-input').value.trim();"
        "  const nonceBytes = new Uint8Array(16);"
        "  crypto.getRandomValues(nonceBytes);"
        "  const nonce = Array.from(nonceBytes).map(b => b.toString(16).padStart(2, '0')).join('');"
        "  const memoPayload = JSON.stringify({app:'onchainbrief', target_sig:targetSig, nonce:nonce});"
        "  const transaction = new solanaWeb3.Transaction().add("
        "    solanaWeb3.SystemProgram.transfer({"
        "      fromPubkey: sender,"
        "      toPubkey: recipient,"
        "      lamports: amountSol * solanaWeb3.LAMPORTS_PER_SOL"
        "    }),"
        "    new solanaWeb3.TransactionInstruction({"
        "      keys: [],"
        "      programId: new solanaWeb3.PublicKey(MEMO_PROGRAM_ID),"
        "      data: new TextEncoder().encode(memoPayload)"
        "    })"
        "  );"
        "  transaction.feePayer = sender;"
        "  transaction.recentBlockhash = blockhash;"
        "  const { signature } = await window.solana.signAndSendTransaction(transaction);"
        "  await connection.confirmTransaction(signature, 'confirmed');"
        "  return {signature, nonce};"
        "}"
        
        "async function submitBriefRequest() {"
        "  const targetSig = document.getElementById('tx-sig-input').value.trim();"
        "  if (!targetSig) {"
        "    showToast('Please enter a Solana transaction signature.', 'warning');"
        "    return;"
        "  }"
        "  const statusMsg = document.getElementById('request-status-msg');"
        "  statusMsg.style.color = 'var(--text-muted)';"
        "  statusMsg.textContent = 'Preparing payment...';"
        "  try {"
        "    if (!userWallet) {"
        "      statusMsg.textContent = 'Please connect wallet first.';"
        "      statusMsg.style.color = 'red';"
        "      return;"
        "    }"
        f"    statusMsg.textContent = 'Prompting payment transfer of {pay_sol} {pay_cluster_label} SOL in Phantom...';"
        f"    const payment = await sendPaymentTx(userWallet, {pay_sol});"
        "    statusMsg.textContent = 'Payment transaction confirmed. Submitting to agent...';"
        "    const response = await fetch('/api/request-brief', {"
        "      method: 'POST',"
        "      headers: { 'Content-Type': 'application/json' },"
        "      body: JSON.stringify({ signature: targetSig, payment_signature: payment.signature, payer_wallet: userWallet })"
        "    });"
        "    const result = await response.json();"
        "    if (response.ok) {"
        "      statusMsg.style.color = 'var(--success)';"
        "      statusMsg.textContent = 'Request accepted! Agent is analyzing transaction signature...';"
        "      document.getElementById('tx-sig-input').value = '';"
        "    } else {"
        "      statusMsg.style.color = 'red';"
        "      statusMsg.textContent = 'Error: ' + result.error;"
        "    }"
        "  } catch (err) {"
        "    console.error(err);"
        "    statusMsg.style.color = 'red';"
        "    statusMsg.textContent = 'Failed: ' + err.message;"
        "  }"
        "}"
        
        "async function refreshFeed(newSig) {"
        "  try {"
        "    const res = await fetch(window.location.href);"
        "    const htmlText = await res.text();"
        "    const parser = new DOMParser();"
        "    const doc = parser.parseFromString(htmlText, 'text/html');"
        "    const newGrid = doc.querySelector('.grid');"
        "    const currentGrid = document.querySelector('.grid');"
        "    const newArticles = newGrid.querySelectorAll('article');"
        "    const currentArticles = currentGrid.querySelectorAll('article');"
        "    const currentSigs = new Set();"
        "    currentArticles.forEach(art => {"
        "      const verifyBtn = art.querySelector('[data-action=\"verify\"]');"
        "      if (verifyBtn) {"
        "        const sig = verifyBtn.getAttribute('data-sig');"
        "        if (sig) currentSigs.add(sig);"
        "      }"
        "    });"
        "    let inserted = false;"
        "    newArticles.forEach(art => {"
        "      const verifyBtn = art.querySelector('[data-action=\"verify\"]');"
        "      if (verifyBtn) {"
        "        const sig = verifyBtn.getAttribute('data-sig');"
        "        if (sig && !currentSigs.has(sig)) {"
        "          art.style.opacity = '0';"
        "          art.style.transform = 'translateY(-10px)';"
        "          if (currentGrid.textContent.includes('No briefs yet')) {"
        "            currentGrid.innerHTML = '';"
        "          }"
        "          currentGrid.insertBefore(art, currentGrid.firstChild);"
        "          art.offsetHeight;"
        "          art.style.transition = 'all 0.5s ease';"
        "          art.style.opacity = '1';"
        "          art.style.transform = 'translateY(0)';"
        "          inserted = true;"
        "        }"
        "      }"
        "    });"
        "  } catch (err) {"
        "    console.error('Failed to refresh feed:', err);"
        "  }"
        "}"
        
        # Single delegated click handler replaces all inline event attributes.
        # Works for dynamically inserted articles (SSE refresh) since it is bound
        # at document level. data-action='noop' on the modal content shields its
        # children so a click inside the modal does not close it.
        "document.addEventListener('click', function(e) {"
        "  const el = e.target.closest('[data-action]');"
        "  if (!el) return;"
        "  const action = el.getAttribute('data-action');"
        "  switch (action) {"
        "    case 'verify':"
        "      verifyBrief(el.getAttribute('data-sig'), el.getAttribute('data-asig'), el.getAttribute('data-cluster'), el.getAttribute('data-card'), el.getAttribute('data-md'));"
        "      break;"
        "    case 'lightbox':"
        "      openLightbox(el.src, el.alt);"
        "      break;"
        "    case 'filter':"
        "      filterCategory(el.getAttribute('data-category'), el);"
        "      break;"
        "    case 'connect-wallet':"
        "      toggleWalletConnect();"
        "      break;"
        "    case 'submit-request':"
        "      submitBriefRequest();"
        "      break;"
        "    case 'close-verify':"
        "      closeVerifyModal();"
        "      break;"
        "    case 'close-lightbox':"
        "      closeLightbox();"
        "      break;"
        "  }"
        "});"
        # A second delegated listener for `input` events (the range slider).
        # Keeps the slider free of any inline oninput so the page carries zero
        # on*= attributes and the CSP can tighten toward dropping unsafe-inline.
        "document.addEventListener('input', function(e) {"
        "  const el = e.target.closest('[data-action]');"
        "  if (!el) return;"
        "  if (el.getAttribute('data-action') === 'usd-filter' && window.applyUsdFilter) {"
        "    window.applyUsdFilter(el.value);"
        "  }"
        "});"
        "const sse = new EventSource('/api/sse');"
        "sse.onmessage = function(event) {"
        "  try {"
        "    const data = JSON.parse(event.data);"
        "    if (data.type === 'new_brief') {"
        "      refreshFeed(data.signature);"
        "    }"
        "  } catch (e) {"
        "    console.warn('Non-JSON event:', event.data);"
        "  }"
        "};"
        "</script>"
        "</body></html>"
    )


# Feed ordering: lead with the most newsworthy, distinct card so the first
# screen is the strongest one — a governance action or a named program upgrade,
# not an anonymous fresh deploy. Ties keep the caller's order (newest first).
_CATEGORY_RANK = {"governance": 0, "security": 1, "volume": 2, "activity": 3,
                  "deployment": 4}


def _feature_rank(item: FeedItem) -> tuple:
    cat_rank = _CATEGORY_RANK.get(item.category.lower(), 3)
    # Within deploys, a named upgrade outranks an anonymous fresh deploy.
    deploy_sub = 0 if "upgrad" in item.headline.lower() else 1
    # Larger value-moves first within a value category.
    return (cat_rank, deploy_sub, -item.amount_usd)


def build_feed(briefs_dir: str | Path, out_html: str | Path) -> Path:
    briefs_dir = Path(briefs_dir)
    out_html = Path(out_html)
    out_dir = out_html.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    # Copy all PNGs and MDs from briefs_dir to out_dir
    for png_path in briefs_dir.glob("*.png"):
        shutil.copy2(png_path, out_dir / png_path.name)
    for md_path in briefs_dir.glob("*.md"):
        shutil.copy2(md_path, out_dir / md_path.name)

    import os
    from solders.keypair import Keypair
    agent_payment_wallet = os.getenv("AGENT_PAYMENT_WALLET", "")
    if not agent_payment_wallet:
        kp_path = os.getenv("SOLANA_KEYPAIR_PATH", "")
        if kp_path and Path(kp_path).exists():
            try:
                kp = Keypair.from_json(Path(kp_path).read_text(encoding="utf-8"))
                agent_payment_wallet = str(kp.pubkey())
            except Exception:
                pass

    mds = sorted(
        briefs_dir.glob("*.md"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    items = [it for p in mds if (it := _parse_brief(p))]
    # Stable significance sort: strongest card first, mtime order within ties.
    items.sort(key=_feature_rank)
    out_html.write_text(_render(items, agent_payment_wallet), encoding="utf-8")
    return out_html
