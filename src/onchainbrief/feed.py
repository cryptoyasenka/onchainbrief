"""Static feed page — the public-facing output.

Scans a briefs directory (the .md + .png pairs that pipeline.py writes) and
renders one index.html with premium design. Copies all PNG cards to the output
directory so that images are not broken on deployment.
"""

from __future__ import annotations

import html
import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import DEVNET_RPC_URL, MAINNET_RPC_URL

_CARD_RE = re.compile(r"!\[card\]\((.+?)\)")
_SIG_RE = re.compile(r"Solana tx `([^`]+)`")
_ATTEST_RE = re.compile(r"Attestation tx `([^`]+)` \(([^)]+)\)")
_SRC_RE = re.compile(r"^- (\S+)", re.MULTILINE)
_CAT_RE = re.compile(r"Category: (\S+)")


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
        cat_badge_html = f'<span class="category-badge {cat.lower()}">{cat}</span>'

        cards.append(
            f'<article data-category="{cat.lower()}">'
            f'{img_html}'
            f'<div class="card-meta">'
            f'{cat_badge_html}'
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
    request_section = ""
    if agent_payment_wallet:
        request_section = (
            "<section class='request-brief-section'>"
            "<div class='request-card'>"
            "<h3>Request an On-Demand Brief</h3>"
            "<p>Submit a Solana transaction signature. The payment transaction includes a Memo binding it to that request, and the server rejects reused payments.</p>"
            "<div class='request-form'>"
            "<input type='text' id='tx-sig-input' placeholder='Enter Solana Transaction Signature' />"
            "<button id='connect-wallet-btn' data-action='connect-wallet'>Connect Phantom</button>"
            "<button id='submit-request-btn' data-action='submit-request' disabled>Request Brief (0.001 SOL)</button>"
            "</div>"
            "<div id='wallet-status' class='wallet-status-idle'>Wallet not connected</div>"
            "<div id='request-status-msg' style='margin-top: 12px; font-size: 12px; font-family: monospace;'></div>"
            "</div>"
            "</section>"
        )

    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>OnchainBrief — Verified Solana Intelligence</title>"
        "<script src='https://unpkg.com/@solana/web3.js@1.98.4/lib/index.iife.min.js'></script>"
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
        
        "/* New Overhaul CSS styles */"
        ".card-meta{display:flex;justify-content:flex-start;align-items:center;margin-bottom:12px}"
        ".category-badge{display:inline-flex;align-items:center;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:0.06em;padding:4px 8px;border-radius:6px;border:1px solid}"
        ".category-badge.security{color:#f87171;border-color:rgba(239,68,68,0.25);background:rgba(239,68,68,0.1)}"
        ".category-badge.deployment{color:#60a5fa;border-color:rgba(59,130,246,0.25);background:rgba(59,130,246,0.1)}"
        ".category-badge.volume{color:#34d399;border-color:rgba(16,185,129,0.25);background:rgba(16,185,129,0.1)}"
        ".category-badge.governance{color:#c084fc;border-color:rgba(139,92,246,0.25);background:rgba(139,92,246,0.1)}"
        ".category-badge.activity{color:#9ca3af;border-color:rgba(156,163,175,0.25);background:rgba(156,163,175,0.1)}"
        
        ".filter-bar{display:flex;justify-content:center;gap:10px;margin-bottom:40px;flex-wrap:wrap}"
        ".filter-btn{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);color:var(--text-muted);padding:8px 16px;border-radius:999px;font-size:12px;font-weight:600;cursor:pointer;transition:all 0.2s cubic-bezier(0.4,0,0.2,1);text-transform:uppercase;letter-spacing:0.04em}"
        ".filter-btn:hover{background:rgba(255,255,255,0.08);border-color:rgba(255,255,255,0.15);color:#ffffff}"
        ".filter-btn.active{background:var(--primary);border-color:var(--primary);color:#ffffff;box-shadow:0 0 15px rgba(139,92,246,0.4)}"
        
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
        "<p class=hero-desc>OnchainBrief watches Solana mainnet for the moves that matter — large transfers, new program deployments, big swaps, governance actions — and turns each into a one-glance, fact-checked brief anchored on-chain for provenance.</p>"
        "</section>"
        
        f"{request_section}"
        
        "<section class=value-grid>"
        "<div class='value-card v1'>"
        "<div class=value-question>WHAT IS ONCHAINBRIEF?</div>"
        "<h3>Real-time Event Watcher</h3>"
        "<p>An autonomous AI agent that streams the Solana ledger and surfaces the moves that matter — whale transfers, new program deployments, large swaps, and governance actions — out of millions of routine transactions.</p>"
        "</div>"
        "<div class='value-card v2'>"
        "<div class=value-question>WHY DOES IT EXIST?</div>"
        "<h3>Facts, Not Noise</h3>"
        "<p>On-chain logs are dense and unreadable. The agent decodes the real numbers — amount, asset, who → whom — fetches web context for the entities, and writes a two-line brief: what happened, and why it matters. No hype.</p>"
        "</div>"
        "<div class='value-card v3'>"
        "<div class=value-question>WHAT VALUE DOES IT BRING?</div>"
        "<h3>Verifiable Ledger Provenance</h3>"
        "<p>Trust is built-in. Every brief is cryptographically signed and attested to Solana's ledger via Memo & SAP protocol, producing a permanent, tamper-proof history of on-chain events.</p>"
        "</div>"
        "</section>"
        
        # Filter tabs
        "<div class='filter-bar'>"
        "<button class='filter-btn active' data-action='filter' data-category='all'>All</button>"
        "<button class='filter-btn' data-action='filter' data-category='security'>Security</button>"
        "<button class='filter-btn' data-action='filter' data-category='deployment'>Deployments</button>"
        "<button class='filter-btn' data-action='filter' data-category='volume'>Volume</button>"
        "<button class='filter-btn' data-action='filter' data-category='governance'>Governance</button>"
        "<button class='filter-btn' data-action='filter' data-category='activity'>Activity</button>"
        "</div>"
        
        f"<main class=grid>{body}</main>"
        "</div>"
        "<footer>"
        "<p>Powered by ACE Services & Solana. Attested via Agent PDA. <a href='https://github.com/cryptoyasenka/oobe-ace-bounty' target=_blank rel='noopener noreferrer'>View Source</a></p>"
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
        
        "function filterCategory(cat, btnEl) {"
        "  document.querySelectorAll('.filter-btn').forEach(btn => btn.classList.remove('active'));"
        "  if (btnEl) btnEl.classList.add('active');"
        "  document.querySelectorAll('.grid article').forEach(card => {"
        "    const cardCat = card.getAttribute('data-category');"
        "    if (cat === 'all' || cardCat === cat) {"
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
        "}"
        
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
        f"  const rpcUrl = '{DEVNET_RPC_URL}';"
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
        "    statusMsg.textContent = 'Prompting payment transfer of 0.001 SOL in Phantom...';"
        "    const payment = await sendPaymentTx(userWallet, 0.001);"
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
    out_html.write_text(_render(items, agent_payment_wallet), encoding="utf-8")
    return out_html
