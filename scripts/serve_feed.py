"""Tiny static server for the OnchainBrief feed (Railway start command).

Serves the built ./site directory (index.html + brief cards). Stdlib only —
no framework, no stdlib-external deps. The build step (scripts/build_site.py)
is cold-start safe, so this serves a valid "No briefs yet" page even before
the first funded brief.

PORT is taken from the environment (Railway injects it). The bind host
defaults to 0.0.0.0 ONLY because a Railway container must be reachable by
the platform proxy — that is the documented, deliberate exception to the
"prefer 127.0.0.1" rule; override with HOST=127.0.0.1 for local use.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import os
import pathlib
import socketserver

SITE_DIR = pathlib.Path(os.getenv("SITE_DIR", "./site")).resolve()
PORT = int(os.getenv("PORT", "8000"))
HOST = os.getenv("HOST", "0.0.0.0")  # Railway requires external bind


def main() -> int:
    SITE_DIR.mkdir(parents=True, exist_ok=True)
    index = SITE_DIR / "index.html"
    if not index.exists():
        index.write_text(
            "<!doctype html><html><body>No briefs yet — run the build "
            "step (scripts/build_site.py).</body></html>",
            encoding="utf-8",
        )

    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(SITE_DIR)
    )

    class Server(socketserver.TCPServer):
        allow_reuse_address = True

    with Server((HOST, PORT), handler) as httpd:
        print(f"OnchainBrief feed on http://{HOST}:{PORT} (dir={SITE_DIR})")
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
