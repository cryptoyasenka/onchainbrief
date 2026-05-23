"""Build the static feed site from produced briefs.

Wraps onchainbrief.feed.build_feed: scans a briefs directory (the .md + .png
pairs the pipeline writes) and renders one self-contained site/index.html.
This is the Railway build step (static output, no server, not Vercel).

Usage:
    python scripts/build_site.py                       # briefs/ -> site/index.html
    python scripts/build_site.py --briefs DIR --out FILE
"""

from __future__ import annotations

import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onchainbrief.feed import build_feed  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the OnchainBrief static feed.")
    ap.add_argument(
        "--briefs",
        default=str(ROOT / "briefs"),
        help="directory of produced briefs (default: ./briefs)",
    )
    ap.add_argument(
        "--out",
        default=str(ROOT / "site" / "index.html"),
        help="output html path (default: ./site/index.html)",
    )
    args = ap.parse_args(argv)

    briefs = pathlib.Path(args.briefs)
    if not briefs.is_dir():
        # An empty/missing briefs dir is not an error: build_feed still emits
        # a valid "No briefs yet" page so the deploy never breaks on a cold
        # start. Create it so the scan has something to look at.
        briefs.mkdir(parents=True, exist_ok=True)

    out = build_feed(briefs, args.out)
    n = len(list(briefs.glob("*.md")))
    print(f"Built {out} from {n} brief(s) in {briefs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
