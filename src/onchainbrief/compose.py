"""Local artifact composer — no ACE, no funds.

Takes the ACE-generated base visual + the ACE-written narrative + the source
event, and produces the two published artifacts:

  * a "trading card" PNG (base image + branded overlay), and
  * a markdown brief with the on-chain signature as provenance.

Composite pattern: the model produces the base visual, local code lays
type/brand on top deterministically (reproducible, no per-render model
cost). Fonts resolve through a fallback list so this also runs on Railway
(Linux), not just Windows. Taglines carry no trailing period.
"""

from __future__ import annotations

import io
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

CARD_W, CARD_H = 1344, 768
_FONT_CANDIDATES = {
    "bold": ["arialbd.ttf", "DejaVuSans-Bold.ttf", "segoeuib.ttf"],
    "regular": ["arial.ttf", "DejaVuSans.ttf", "segoeui.ttf"],
    "mono": ["consola.ttf", "DejaVuSansMono.ttf", "cour.ttf"],
}


def _font(kind: str, size: int) -> ImageFont.FreeTypeFont:
    import pathlib
    local_font_dir = pathlib.Path(__file__).resolve().parent / "assets" / "fonts"
    local_name = None
    if kind == "bold":
        local_name = "Inter-Bold.ttf"
    elif kind == "regular":
        local_name = "Inter-Regular.ttf"
    elif kind == "mono":
        local_name = "RobotoMono-Regular.ttf"

    if local_name:
        local_path = local_font_dir / local_name
        if local_path.exists():
            try:
                return ImageFont.truetype(str(local_path), size)
            except OSError:
                pass

    for name in _FONT_CANDIDATES[kind]:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _strip_period(s: str) -> str:
    s = s.strip()
    return s[:-1] if s.endswith(".") else s


def compose_card(
    base_image: bytes | str | Path,
    headline: str,
    subline: str,
    signature: str,
    out_path: str | Path,
    *,
    attestation: tuple[str, str] | None = None,
) -> Path:
    if isinstance(base_image, bytes):
        base = Image.open(io.BytesIO(base_image)).convert("RGBA")
    else:
        base = Image.open(base_image).convert("RGBA")
    base = base.resize((CARD_W, CARD_H))

    # Fresh dark canvas
    card = Image.new("RGBA", (CARD_W, CARD_H), (8, 10, 16, 255))
    d = ImageDraw.Draw(card)

    # 1. Tech grid & corner decorations
    d.rectangle([12, 12, CARD_W - 12, CARD_H - 12], outline=(30, 41, 59, 150), width=1)
    
    # Corner brackets for the entire card
    bracket_color = (139, 92, 246, 120)  # Neon purple
    d.line([(12, 32), (12, 12), (32, 12)], fill=bracket_color, width=2)
    d.line([(CARD_W - 12, 32), (CARD_W - 12, 12), (CARD_W - 32, 12)], fill=bracket_color, width=2)
    d.line([(12, CARD_H - 32), (12, CARD_H - 12), (32, CARD_H - 12)], fill=bracket_color, width=2)
    d.line([(CARD_W - 12, CARD_H - 32), (CARD_W - 12, CARD_H - 12), (CARD_W - 32, CARD_H - 12)], fill=bracket_color, width=2)

    # 2. Crop and paste the top section of the base image (guaranteed clean visual)
    art_x0, art_y0 = 48, 48
    art_x1, art_y1 = CARD_W - 48, 430
    cropped = base.crop((art_x0, art_y0, art_x1, art_y1))
    card.paste(cropped, (art_x0, art_y0), cropped)

    # Draw the frame border
    d.rectangle([art_x0, art_y0, art_x1, art_y1], outline=(30, 41, 59, 255), width=2)

    # Neon cyan corners around the visual feed frame
    c_color = (6, 182, 212, 255)  # Cyan
    d.line([(art_x0 - 4, art_y0 - 4), (art_x0 + 20, art_y0 - 4)], fill=c_color, width=3)
    d.line([(art_x0 - 4, art_y0 - 4), (art_x0 - 4, art_y0 + 20)], fill=c_color, width=3)
    d.line([(art_x1 + 4, art_y0 - 4), (art_x1 - 20, art_y0 - 4)], fill=c_color, width=3)
    d.line([(art_x1 + 4, art_y0 - 4), (art_x1 + 4, art_y0 + 20)], fill=c_color, width=3)
    d.line([(art_x0 - 4, art_y1 + 4), (art_x0 + 20, art_y1 + 4)], fill=c_color, width=3)
    d.line([(art_x0 - 4, art_y1 + 4), (art_x0 - 4, art_y1 - 20)], fill=c_color, width=3)
    d.line([(art_x1 + 4, art_y1 + 4), (art_x1 - 20, art_y1 + 4)], fill=c_color, width=3)
    d.line([(art_x1 + 4, art_y1 + 4), (art_x1 + 4, art_y1 - 20)], fill=c_color, width=3)

    # Labels for sci-fi atmosphere
    d.text((art_x0 + 10, art_y0 - 25), "NEURAL VISUAL FEED // UNTAMPED DATA", font=_font("mono", 11), fill=(6, 182, 212, 180))

    # 3. Information Console (Bottom Section)
    headline_text = _strip_period(headline).upper()
    font_size = 42
    font = _font("bold", font_size)
    try:
        while d.textlength(headline_text, font=font) > (art_x1 - art_x0 - 40) and font_size > 28:
            font_size -= 2
            font = _font("bold", font_size)
    except (AttributeError, TypeError):
        if len(headline_text) > 30:
            font_size = max(28, int(42 * 30 / len(headline_text)))
            font = _font("bold", font_size)

    d.text((art_x0, 465), headline_text, font=font, fill=(255, 255, 255))

    # Decorative separator line
    sep_y = 525
    d.line([(art_x0, sep_y), (art_x1, sep_y)], fill=(139, 92, 246, 100), width=1)
    d.rectangle([art_x0, sep_y - 2, art_x0 + 6, sep_y + 2], fill=(139, 92, 246, 255))
    d.rectangle([art_x1 - 6, sep_y - 2, art_x1, sep_y + 2], fill=(139, 92, 246, 255))

    # Two-column layout details
    content_y = 548
    
    # Left Column: wrapped narrative text
    subline_text = _strip_period(subline)
    subline_lines = textwrap.wrap(subline_text, width=54)
    if len(subline_lines) > 3:
        subline_lines = subline_lines[:3]
        if not subline_lines[2].endswith("…"):
            subline_lines[2] = subline_lines[2].rstrip() + "…"
            
    for n, line in enumerate(subline_lines):
        d.text((art_x0, content_y + n * 36), line, font=_font("regular", 22), fill=(209, 213, 219))

    # Vertical tech separator
    col2_x = 830
    d.line([(col2_x, content_y), (col2_x, CARD_H - 60)], fill=(30, 41, 59, 150), width=1)

    # Right Column: metadata stats
    meta_x = col2_x + 30
    labels = ["EVENT SIG", "ATTESTATION", "NETWORK", "SECURITY"]
    
    sig_str = f"{signature[:16]}...{signature[-8:]}"
    if attestation is not None:
        attest_sig, cluster = attestation
        attest_str = f"{attest_sig[:12]}... ({cluster})"
        attest_color = (52, 211, 153)  # Emerald green
    else:
        attest_str = "PENDING REGISTRATION"
        attest_color = (239, 68, 68)   # Red
        
    values = [
        (sig_str, (103, 232, 249)),    # Cyan
        (attest_str, attest_color),
        ("SOLANA MAINNET", (255, 255, 255)),
        ("SAP VERIFIED", (139, 92, 246)), # Purple
    ]
    
    for i, (label, (val, color)) in enumerate(zip(labels, values)):
        curr_y = content_y + i * 36
        d.text((meta_x, curr_y), f"{label:<12}:", font=_font("mono", 18), fill=(156, 163, 175))
        d.text((meta_x + 140, curr_y), val, font=_font("mono", 18), fill=color)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    card.convert("RGB").save(out_path, "PNG")
    return out_path


def write_brief_markdown(
    headline: str,
    narrative: str,
    sources: list[str],
    signature: str,
    card_path: str | Path,
    out_path: str | Path,
    *,
    attestation: tuple[str, str] | None = None,
) -> Path:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = "\n".join(f"- {s}" for s in sources) or "- (none captured)"
    attest_line = ""
    if attestation is not None:
        attest_sig, cluster = attestation
        explorer = (
            f"https://solscan.io/tx/{attest_sig}"
            if cluster == "mainnet-beta"
            else f"https://solscan.io/tx/{attest_sig}?cluster={cluster}"
        )
        attest_line = f"\nAttestation tx `{attest_sig}` ({cluster}) — {explorer}\n"
    out_path.write_text(
        f"# {_strip_period(headline)}\n\n"
        f"![card]({Path(card_path).name})\n\n"
        f"{narrative.strip()}\n\n"
        f"## Sources\n{src}\n\n"
        f"## Provenance\n"
        f"Solana tx `{signature}` — "
        f"https://solscan.io/tx/{signature}\n"
        f"{attest_line}",
        encoding="utf-8",
    )
    return out_path
