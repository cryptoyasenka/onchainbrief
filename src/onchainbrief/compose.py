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
        base = Image.open(io.BytesIO(base_image)).convert("RGB")
    else:
        base = Image.open(base_image).convert("RGB")
    base = base.resize((CARD_W, CARD_H))

    # Bottom gradient scrim so type stays legible over any visual.
    scrim = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for i in range(360):
        a = int(200 * (i / 360) ** 1.4)
        sd.line([(0, CARD_H - 360 + i), (CARD_W, CARD_H - 360 + i)],
                fill=(8, 10, 16, a))
    card = Image.alpha_composite(base.convert("RGBA"), scrim)

    # Rounded dark glass container at the bottom with a neon border
    box_x0 = 48
    box_y0 = CARD_H - 340
    box_x1 = CARD_W - 48
    box_y1 = CARD_H - 48
    
    overlay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    
    # Draw dark container (94% opaque) with a sleek neon border
    od.rounded_rectangle(
        [box_x0, box_y0, box_x1, box_y1],
        radius=18,
        fill=(8, 10, 16, 240),
        outline=(139, 92, 246, 220), # Neon purple
        width=3
    )
    
    card = Image.alpha_composite(card, overlay)
    d = ImageDraw.Draw(card)

    # 1. Headline
    headline_text = _strip_period(headline).upper()
    font_size = 54
    font = _font("bold", font_size)
    try:
        while d.textlength(headline_text, font=font) > (box_x1 - box_x0 - 80) and font_size > 36:
            font_size -= 2
            font = _font("bold", font_size)
    except (AttributeError, TypeError):
        if len(headline_text) > 22:
            font_size = max(36, int(54 * 22 / len(headline_text)))
            font = _font("bold", font_size)

    text_x = box_x0 + 40
    d.text((text_x, box_y0 + 32), headline_text, font=font, fill=(255, 255, 255))

    # 2. Subline
    subline_lines = textwrap.wrap(_strip_period(subline), width=75)
    if len(subline_lines) > 2:
        subline_lines = subline_lines[:2]
        if not subline_lines[1].endswith("…"):
            subline_lines[1] = subline_lines[1].rstrip() + "…"

    for n, line in enumerate(subline_lines):
        d.text((text_x, box_y0 + 104 + n * 40), line,
               font=_font("regular", 28), fill=(209, 213, 219))

    # 3. Signatures / Metadata
    sig_y = box_y1 - 76
    d.text((text_x, sig_y), f"sig: {signature[:32]}…",
           font=_font("mono", 20), fill=(103, 232, 249))

    if attestation is not None:
        attest_sig, cluster = attestation
        attest_y = box_y1 - 44
        d.text((text_x, attest_y), f"attest ({cluster}): {attest_sig[:32]}…",
               font=_font("mono", 18), fill=(52, 211, 153))

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
