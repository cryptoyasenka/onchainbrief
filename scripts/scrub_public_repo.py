"""Publish only an explicit allowlist; hard-gate it before any public push.

The working tree carries local notes under `.planning/` that are not part of
the deliverable. This builds a clean public tree from an explicit allowlist
(source, tests, scripts, packaging) and refuses if any internal marker
survives, so private working material can never reach the public repo.

    python scripts/scrub_public_repo.py            # build + gate ./.public-build
    python scripts/scrub_public_repo.py --check     # gate only, no copy
    python scripts/scrub_public_repo.py --dest DIR

It never pushes. After a PASS, init a fresh repo from the dest (or an orphan
branch) and push — history must not include old commits or `.planning/`.
"""

from __future__ import annotations

import argparse
import base64
import os
import pathlib
import shutil
import stat
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]

# The only paths that may reach the public repo: source, tests, the scripts,
# and packaging/deploy config. Everything else stays local.
ALLOW = [
    "src", "tests", "scripts", "sap",
    "README.md", "SECURITY.md", "requirements.txt", "pyproject.toml",
    ".env.example", ".gitignore", "railway.toml", "Procfile",
    ".github",
]
# Substrings that must never reach the public tree (case-insensitive).
# Base64-encoded so this tool — itself in the public allowlist — does not
# leak the very terms it guards against (the gate would flag itself).
# Mix of internal project codenames/handles, the operator's personal first
# name, and a credential tripwire. The personal-name entry catches a stray
# "<name>'s call"-style aside leaking into an allowlisted script; the last
# entry decodes to the OOBE/Stripe-style live secret-key prefix, catching such
# a key accidentally pasted into an allowlisted source/script file. Every term
# is kept base64-only here too — writing one in plaintext would make this gate
# flag itself (scripts/ is in the allowlist).
DENY = [
    base64.b64decode(b).decode()
    for b in ("Y3VzdG9z", "Ym9vemVsZWU=", "aHVhbmd6ZXNlbg==", "MWFybGV5c29s",
              "eWFuYQ==", "c2tfbGl2ZQ==")
]
SKIP_DIRS = {"__pycache__", ".state", "briefs", "site", "demo-out",
             ".pytest_cache", ".public-build", ".git", ".planning",
             "node_modules", "dist"}
SKIP_SUFFIX = {".pyc", ".png", ".jpg", ".jpeg", ".webp", ".ico",
               ".tsbuildinfo"}


def _iter_files(base: pathlib.Path):
    for p in base.rglob("*"):
        if p.is_dir():
            continue
        # Skip relative to base — the dest's own name (e.g. .public-build)
        # is in SKIP_DIRS and must NOT vacuously empty the scan.
        if any(part in SKIP_DIRS for part in p.relative_to(base).parts):
            continue
        if p.suffix.lower() in SKIP_SUFFIX:
            continue
        yield p


def _scan_file(p: pathlib.Path, label: str) -> list[str]:
    if p.suffix.lower() in SKIP_SUFFIX:
        return []
    try:
        text = p.read_text(encoding="utf-8", errors="ignore").lower()
    except OSError:
        return []
    return [f"{label} :: contains {t!r}" for t in DENY if t in text]


def _scan(base: pathlib.Path) -> list[str]:
    """Scan a file or a directory tree for any DENY term."""
    if base.is_file():
        return _scan_file(base, base.name)
    hits: list[str] = []
    for p in _iter_files(base):
        hits += _scan_file(p, str(p.relative_to(base)))
    return hits


def _force_rmtree(path: pathlib.Path) -> None:
    """Windows-safe: clear the read-only bit and retry (WinError 5).

    `onexc` is 3.12+; `onerror` is the pre-3.12 name. pyproject floors at
    3.10, so pick the kwarg the running interpreter actually accepts (the
    handler ignores the differing 3rd arg — exc instance vs exc_info tuple).
    """
    def _handler(func, p, _third):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    kw = "onexc" if sys.version_info >= (3, 12) else "onerror"
    shutil.rmtree(path, **{kw: _handler})


def _copy_allowlist(dest: pathlib.Path) -> None:
    if dest.exists():
        _force_rmtree(dest)
    dest.mkdir(parents=True)

    def _ignore(_d, names):
        return [n for n in names
                if n in SKIP_DIRS or pathlib.Path(n).suffix.lower() == ".pyc"]

    for name in ALLOW:
        src = ROOT / name
        if not src.exists():
            print(f"  ! allowlist entry missing (skipped): {name}")
            continue
        target = dest / name
        if src.is_dir():
            shutil.copytree(src, target, ignore=_ignore)
        else:
            shutil.copy2(src, target)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="public-repo leak gate")
    ap.add_argument("--dest", default=str(ROOT / ".public-build"))
    ap.add_argument("--check", action="store_true",
                    help="scan the in-repo allowlist only; do not copy")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    if args.check:
        # Gate the would-be-published files in place (no .planning/).
        # _scan already handles file-or-dir, so this is just a sweep.
        hits: list[str] = []
        for name in ALLOW:
            p = ROOT / name
            if p.exists():
                hits += _scan(p)
        if hits:
            print("LEAK — internal terms in the public allowlist:")
            print("\n".join(f"  {h}" for h in hits))
            return 2
        # Never echo the decoded DENY terms — this tool ships in the public
        # allowlist, and stdout can land in CI logs / pasted output.
        print(f"PASS (check): {len(ALLOW)} allowlist entries clean, "
              f"no internal terms ({len(DENY)} guarded) leaked. "
              f".planning/ excluded by design.")
        return 0

    dest = pathlib.Path(args.dest)
    print(f"Building clean public tree -> {dest}")
    _copy_allowlist(dest)
    hits = _scan(dest)
    if hits:
        print("LEAK — aborting, dest removed. Offenders:")
        print("\n".join(f"  {h}" for h in hits))
        shutil.rmtree(dest, ignore_errors=True)
        return 2

    n = sum(1 for _ in _iter_files(dest))
    print(f"PASS: {n} files, no internal terms ({len(DENY)} guarded) "
          f"leaked, .planning/ excluded.")
    print("\nNext (manual — this script never pushes):")
    print(f"  cd {dest}")
    print("  git init && git add -A && git commit -m 'OnchainBrief'")
    print("  gh repo create onchainbrief --public --source=. --push")
    print("  (fresh history — no old commits / reflog / .planning)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
