"""Local artifact composer - no ACE, no funds.

Takes the ACE-generated base visual + the decoded event facts + the source
signature, and produces the two published artifacts:

  * a "trading card" PNG - the base image with a real data overlay drawn on
    top (headline, the amount/USD figure, program, category badge, short
    signature), so the card reports the event instead of being decoration, and
  * a markdown brief with the on-chain signature as provenance.

Composite pattern: the model produces the base visual, local code lays the
type + data on top deterministically (reproducible, no per-render model
cost). Fonts resolve through a fallback list so this also runs on Railway
(Linux), not just Windows. Taglines carry no trailing period.
"""

from __future__ import annotations

import io
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


# Category badge colours (match the feed CSS palette).
_CAT_COLORS = {
    "Security": (248, 113, 113),
    "Deployment": (96, 165, 250),
    "Volume": (52, 211, 153),
    "Governance": (192, 132, 252),
    "Activity": (156, 163, 175),
}


def _fit_font(draw, text, kind, max_w, start, min_size=28):
    """Largest font of `kind` whose `text` fits in `max_w` (down to min_size)."""
    size = start
    while size > min_size:
        f = _font(kind, size)
        if draw.textlength(text, font=f) <= max_w:
            return f
        size -= 4
    return _font(kind, min_size)


def _metric_line(facts) -> str:
    """The headline number - e.g. '5,000 SOL  ·  $750K'."""
    if facts is None or not getattr(facts, "has_value", False):
        return ""
    amt = f"{facts.amount_native:,.4f}".rstrip("0").rstrip(".")
    line = f"{amt} {facts.asset}"
    if getattr(facts, "amount_usd", None):
        usd = facts.amount_usd
        usd_s = (
            f"${usd / 1_000_000:.2f}M" if usd >= 1_000_000
            else f"${usd / 1_000:.0f}K" if usd >= 1_000
            else f"${usd:,.0f}"
        )
        line += f"  ·  {usd_s}"
    return line


def _headline_already_carries_metric(headline: str, metric: str) -> bool:
    """Avoid printing the same amount twice on generated visual cards."""
    if not headline or not metric:
        return False
    head = headline.lower().replace(",", "")
    metric_head = metric.split("  ", 1)[0].lower().replace(",", "")
    parts = metric_head.split()
    if len(parts) < 2:
        return False
    amount, asset = parts[0], parts[1]
    return amount in head and asset in head


def compose_card(
    base_image: bytes | str | Path,
    headline: str,
    subline: str,
    signature: str,
    out_path: str | Path,
    *,
    attestation: tuple[str, str] | None = None,
    facts: object | None = None,
    category: str | None = None,
) -> Path:
    if isinstance(base_image, bytes):
        base = Image.open(io.BytesIO(base_image)).convert("RGB")
    else:
        base = Image.open(base_image).convert("RGB")

    # Crop to correct aspect ratio without distortion, then size to the card.
    target_ratio = CARD_W / CARD_H
    base = crop_to_aspect_ratio(base, target_ratio)
    base = base.resize((CARD_W, CARD_H)).convert("RGBA")

    pad = 56
    overlay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)

    # Bottom scrim so text stays legible over any base image.
    scrim_top = int(CARD_H * 0.42)
    for y in range(scrim_top, CARD_H):
        a = int(225 * (y - scrim_top) / (CARD_H - scrim_top))
        od.line([(0, y), (CARD_W, y)], fill=(5, 8, 18, a))

    # Category badge, top-left. Keep it quiet: the feed already exposes filters.
    if category:
        col = _CAT_COLORS.get(category, _CAT_COLORS["Activity"])
        bf = _font("bold", 22)
        label = category.upper()
        tw = od.textlength(label, font=bf)
        od.rounded_rectangle(
            [pad, pad, pad + tw + 30, pad + 38], radius=10,
            fill=(col[0], col[1], col[2], 28),
            outline=(col[0], col[1], col[2], 150), width=2,
        )
        od.text((pad + 15, pad + 8), label, font=bf, fill=col)

    # Headline (auto-scaled, single line, ellipsised if it still overflows).
    head = _strip_period(headline).upper()
    max_w = CARD_W - 2 * pad
    hf = _fit_font(od, head, "bold", max_w, start=76, min_size=34)
    while head and od.textlength(head, font=hf) > max_w:
        head = head[:-2]
        disp = head + "…"
        if od.textlength(disp, font=hf) <= max_w:
            head = disp
            break
    metric = _metric_line(facts)
    if _headline_already_carries_metric(head, metric):
        metric = ""
    mf = _fit_font(od, metric, "bold", max_w, start=64, min_size=34) if metric else None

    # Stack from the bottom up: meta -> optional metric -> headline.
    y = CARD_H - pad

    # Meta line: program + UTC time (from facts.block_time).
    meta_bits = []
    if facts is not None and getattr(facts, "program_name", ""):
        meta_bits.append(facts.program_name)
    bt = getattr(facts, "block_time", None) if facts is not None else None
    if bt:
        import datetime
        meta_bits.append(
            datetime.datetime.fromtimestamp(bt, datetime.timezone.utc)
            .strftime("%Y-%m-%d %H:%M UTC")
        )
    if meta_bits:
        rf = _font("regular", 24)
        y -= 34
        od.text((pad, y), "  ·  ".join(meta_bits), font=rf, fill=(186, 196, 210, 230))

    if mf is not None:
        y -= int(mf.size * 1.25)
        od.text((pad, y), metric, font=mf, fill=(16, 185, 129, 255))

    y -= int(hf.size * 1.2)
    od.text((pad, y), head, font=hf, fill=(255, 255, 255, 255))

    # Attestation receipt tag, top-right.
    if attestation is not None:
        af = _font("bold", 22)
        tag = "✓ ATTESTED ON-CHAIN"
        tw = od.textlength(tag, font=af)
        od.rounded_rectangle(
            [CARD_W - pad - tw - 32, pad, CARD_W - pad, pad + 40], radius=10,
            fill=(16, 185, 129, 36), outline=(16, 185, 129, 200), width=2,
        )
        od.text((CARD_W - pad - tw - 16, pad + 8), tag, font=af, fill=(52, 211, 153))

    card = Image.alpha_composite(base, overlay).convert("RGB")
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    card.save(out_path, "PNG")
    return out_path


def write_brief_markdown(
    headline: str,
    narrative: str,
    sources: list[str],
    signature: str,
    card_path: str | Path | None,
    out_path: str | Path,
    *,
    attestation: tuple[str, str] | None = None,
    category: str | None = None,
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
    card_line = f"![card]({Path(card_path).name})\n\n" if card_path else ""
    category_line = f"Category: {category}\n" if category else ""
    out_path.write_text(
        f"# {_strip_period(headline)}\n\n"
        f"{card_line}"
        f"{narrative.strip()}\n\n"
        f"## Sources\n{src}\n\n"
        f"## Provenance\n"
        f"Solana tx `{signature}` — "
        f"https://solscan.io/tx/{signature}\n"
        f"{category_line}"
        f"{attest_line}",
        encoding="utf-8",
    )
    return out_path
