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


def crop_to_aspect_ratio(img: Image.Image, target_ratio: float) -> Image.Image:
    w, h = img.size
    current_ratio = w / h
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        x0 = (w - new_w) // 2
        return img.crop((x0, 0, x0 + new_w, h))
    elif current_ratio < target_ratio:
        new_h = int(w / target_ratio)
        y0 = (h - new_h) // 2
        return img.crop((0, y0, w, y0 + new_h))
    return img


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
        
    # Crop to correct aspect ratio without distortion
    target_ratio = CARD_W / CARD_H
    base = crop_to_aspect_ratio(base, target_ratio)
    base = base.resize((CARD_W, CARD_H))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    base.save(out_path, "PNG")
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

