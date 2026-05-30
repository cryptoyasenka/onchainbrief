#!/usr/bin/env python
"""One-shot pre-submission gate for OnchainBrief.

Runs every objective check a judge (or we) would re-run, so "ready to submit"
is a fact instead of a vibe. Read-only and free: no network spend, no on-chain
writes, no publish. Exits non-zero if any gate fails.

Usage:
    python scripts/final_gate.py

Gates:
    1. pytest            - the offline regression suite.
    2. tsc --noEmit      - SAP TypeScript type-checks (sap/, if installed).
    3. npm audit:ci      - SAP dependency high/critical gate (sap/, if installed).
    4. scrub --check     - the public allowlist carries no internal terms.
    5. tracked secrets   - no tracked .env/keypair/key file and no live-key prefix.
    6. artifact hashes   - each brief's sha256(png || md) == its attest sidecar.
    7. feed card count   - site/index.html article count == number of briefs.
    8. artifact leak-scan - briefs/ + site/ text carry no internal names/secrets
                            (scrub skips them by design, so this covers the gap).
"""
from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAP = ROOT / "sap"
BRIEFS = ROOT / "briefs"
SITE = ROOT / "site" / "index.html"

# The live API-key prefix, kept base64-only so this script (it ships in the
# public allowlist) does not trip the scrub gate or its own scan.
_LIVE_KEY_PREFIX = base64.b64decode(b"c2tfbGl2ZQ==").decode()
_SKIP_SUFFIX = {".png", ".jpg", ".jpeg", ".webp", ".ico", ".pyc", ".tsbuildinfo"}
_SELF_SKIP = {"final_gate.py", "scrub_public_repo.py"}


def _run(cmd, *, cwd=None, shell=False, timeout=600):
    """Run a command; return (returncode, combined_output)."""
    try:
        p = subprocess.run(
            cmd, cwd=cwd, shell=shell,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=timeout,
        )
        return p.returncode, p.stdout or ""
    except Exception as e:  # launch failure / timeout
        return 1, f"failed to run {cmd!r}: {e!r}"


def _last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def gate_pytest():
    rc, out = _run([sys.executable, "-m", "pytest", "tests", "-q"], cwd=ROOT)
    return rc == 0, _last_line(out)


def gate_tsc():
    if not (SAP / "node_modules").exists():
        return True, "sap/node_modules absent - skipped (run npm ci in sap/)"
    rc, out = _run("npx tsc --noEmit", cwd=SAP, shell=True)
    return rc == 0, "type-check clean" if rc == 0 else _last_line(out)[:200]


def gate_npm_audit():
    if not (SAP / "node_modules").exists():
        return True, "sap/node_modules absent - skipped (run npm ci in sap/)"
    rc, out = _run("npm run audit:ci", cwd=SAP, shell=True)
    return rc == 0, "no high/critical advisories" if rc == 0 else _last_line(out)[:200]


def gate_scrub():
    rc, out = _run([sys.executable, "scripts/scrub_public_repo.py", "--check"], cwd=ROOT)
    return rc == 0, _last_line(out)


def gate_tracked_secrets():
    rc, out = _run(["git", "ls-files"], cwd=ROOT)
    if rc != 0:
        return False, "git ls-files failed (not a git repo?)"
    files = [f.strip() for f in out.splitlines() if f.strip()]
    problems: list[str] = []

    # (a) Sensitive filenames must never be tracked.
    for rel in files:
        name = Path(rel).name.lower()
        if name == ".env" or "keypair" in name or name in ("id_rsa", "id_ed25519") \
                or name.endswith((".pem", ".key")):
            problems.append(f"sensitive file tracked: {rel}")

    # (b) The live API-key prefix must not appear in any tracked text file.
    for rel in files:
        p = ROOT / rel
        if p.suffix.lower() in _SKIP_SUFFIX or p.name in _SELF_SKIP:
            continue
        try:
            if _LIVE_KEY_PREFIX in p.read_text(encoding="utf-8", errors="ignore"):
                problems.append(f"live-key prefix in {rel}")
        except OSError:
            continue

    if problems:
        return False, "; ".join(problems[:5])
    return True, f"{len(files)} tracked files: no secret files or key prefixes"


def gate_artifact_hashes():
    if not BRIEFS.exists():
        return False, "briefs/ missing"
    sidecars = sorted(BRIEFS.glob("*.attest.json"))
    if not sidecars:
        return False, "no .attest.json sidecars found"
    bad: list[str] = []
    checked = 0
    for sc in sidecars:
        stem = sc.name[: -len(".attest.json")]
        try:
            data = json.loads(sc.read_text(encoding="utf-8"))
        except Exception as e:
            bad.append(f"{stem}: unreadable sidecar ({e!r})")
            continue
        want = data.get("artifact_sha256", "")
        if not want:
            continue  # legacy sidecar without an artifact hash
        png, md = BRIEFS / f"{stem}.png", BRIEFS / f"{stem}.md"
        if not png.exists() or not md.exists():
            bad.append(f"{stem}: missing png/md")
            continue
        h = hashlib.sha256()
        h.update(png.read_bytes())  # png then md - matches attest._artifact_sha256
        h.update(md.read_bytes())
        if h.hexdigest() != want:
            bad.append(f"{stem}: {h.hexdigest()[:12]} != {want[:12]}")
        else:
            checked += 1
    if bad:
        return False, "; ".join(bad)
    return True, f"{checked} brief artifact hash(es) match their sidecar"


def gate_card_count():
    if not SITE.exists():
        return False, "site/index.html missing (run build_site.py)"
    html = SITE.read_text(encoding="utf-8", errors="ignore")
    cards = html.count("<article")
    briefs = len(list(BRIEFS.glob("*.md"))) if BRIEFS.exists() else 0
    ok = cards > 0 and cards == briefs
    return ok, f"{cards} cards rendered vs {briefs} brief(s)"


# briefs/ and site/ are skipped by scrub_public_repo.py by design, so these
# public-facing artifacts get no automated leak check. Scan their TEXT files for
# internal names/secrets. Codenames/personal-name/tooling traces are base64-only
# so this public-shipped script does not itself carry the terms it forbids.
_LEAK_TERMS = [base64.b64decode(b).decode() for b in (
    b"WWFuYQ==",      # personal name
    b"Q3VzdG9z",      # prior codename
    b"Q2xhdWRl",      # tooling trace
)] + ["mnemonic", "seed phrase", "PRIVATE KEY", "C:\\Projects", "C:/Projects", _LIVE_KEY_PREFIX]
_ARTIFACT_TEXT_SUFFIX = {".md", ".html", ".htm", ".json", ".js", ".css", ".txt", ".svg"}


def gate_artifact_text_leak():
    hits: list[str] = []
    scanned = 0
    for root in (BRIEFS, ROOT / "site"):
        if not root.exists():
            continue
        for p in sorted(root.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in _ARTIFACT_TEXT_SUFFIX:
                continue
            scanned += 1
            try:
                text = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for term in _LEAK_TERMS:
                if term and term in text:
                    hits.append(f"{p.relative_to(ROOT)}: '{term[:6]}...'")
    if hits:
        return False, "; ".join(hits[:5])
    return True, f"{scanned} artifact text file(s): no internal names/secrets"


GATES = [
    ("pytest", gate_pytest),
    ("tsc --noEmit", gate_tsc),
    ("npm audit:ci", gate_npm_audit),
    ("scrub --check", gate_scrub),
    ("tracked secrets", gate_tracked_secrets),
    ("artifact hashes", gate_artifact_hashes),
    ("feed card count", gate_card_count),
    ("artifact leak-scan", gate_artifact_text_leak),
]


def main() -> int:
    print("OnchainBrief final gate (read-only, no spend)\n" + "=" * 60)
    results = []
    for name, fn in GATES:
        try:
            ok, detail = fn()
        except Exception as e:  # a gate must never crash the runner
            ok, detail = False, f"gate raised: {e!r}"
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name:<16} {detail}")
    failed = [n for n, ok, _ in results if not ok]
    print("=" * 60)
    if failed:
        print(f"RESULT: FAIL ({len(failed)}/{len(results)} gate(s) failed: {', '.join(failed)})")
        return 1
    print(f"RESULT: PASS (all {len(results)} gates green)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
