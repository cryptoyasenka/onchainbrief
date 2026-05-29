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
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from onchainbrief.feed import build_feed  # noqa: E402
from onchainbrief.proof import build_proof  # noqa: E402

# Env defaults so the same BRIEFS_DIR / SITE_HTML contract used by e2e_demo.py
# and serve_feed.py also drives the build step — e.g. on a Railway deploy that
# serves briefs from a persistent volume, point all three at /data via env and
# the runtime rebuild picks up the volume. An explicit CLI flag still wins.
_DEFAULT_BRIEFS = os.getenv("BRIEFS_DIR", str(ROOT / "briefs"))
_DEFAULT_OUT = os.getenv("SITE_HTML", str(ROOT / "site" / "index.html"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Build the OnchainBrief static feed.")
    ap.add_argument(
        "--briefs",
        default=_DEFAULT_BRIEFS,
        help="directory of produced briefs (default: $BRIEFS_DIR or ./briefs)",
    )
    ap.add_argument(
        "--out",
        default=_DEFAULT_OUT,
        help="output html path (default: $SITE_HTML or ./site/index.html)",
    )
    args = ap.parse_args(argv)

    briefs = pathlib.Path(args.briefs)
    if not briefs.is_dir():
        # An empty/missing briefs dir is not an error: build_feed still emits
        # a valid "No briefs yet" page so the deploy never breaks on a cold
        # start. Create it so the scan has something to look at.
        briefs.mkdir(parents=True, exist_ok=True)

    out = build_feed(briefs, args.out)
    # Emit the public proof manifest alongside the page so the static deploy
    # serves /proof.json (the one-stop audit trail) with no extra route.
    proof = build_proof(briefs, out.parent / "proof.json")
    n = len(list(briefs.glob("*.md")))
    print(f"Built {out} from {n} brief(s) in {briefs}")
    print(f"Wrote proof manifest {proof}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
