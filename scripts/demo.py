"""Offline end-to-end proof that the OnchainBrief agent works.

Runs the EXACT production pipeline (AceBriefClient -> run_brief ->
compose_card -> write_brief_markdown -> build_feed) against a sample Solana
event. The only thing swapped for the real network is the transport: a
deterministic fake that returns ACE's *documented* response shapes
(SerpAPI {"organic":[...]}, OpenAIAPI choices[0].message.content,
NanoBanana {"success","data":[{"image_url"}]}) and a locally-rendered PNG
instead of a CDN download. No funds, no token, no network — anyone can run it.

    python scripts/demo.py            # -> ./demo-out/site/index.html
    python scripts/demo.py --out DIR

When the funded ACE token + Base USDC land, the SAME run_brief path runs
with a real AceClient/X402Client transport — nothing else changes.
"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from PIL import Image  # noqa: E402

from onchainbrief.ace_brief_client import AceBriefClient  # noqa: E402
from onchainbrief.compose import CARD_H, CARD_W  # noqa: E402
from onchainbrief.feed import build_feed  # noqa: E402
from onchainbrief.filter import is_brief_worthy  # noqa: E402
from onchainbrief.pipeline import run_brief  # noqa: E402
from onchainbrief.watcher import SolanaLogWatcher  # noqa: E402

_SAMPLE_NOTIF = {
    "method": "logsNotification",
    "params": {"result": {
        "context": {"slot": 301_452_889},
        "value": {
            "signature": "5Ryac1demoEVENTsig8h2kP9qLmNbVcXz4tQwErTyU1A",
            "err": None,
            "logs": [
                "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 invoke [1]",
                "Program log: Instruction: Route",
                "Program TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA invoke [2]",
                "Program log: Transfer 1284000 USDC",
                "Program ComputeBudget111111111111111111111111111111 invoke [1]",
                "Program log: notable multi-hop swap settled",
                "Program log: fee 0.00005 SOL",
                "Program log: slippage 0.1%",
                "Program JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4 success",
            ],
        },
    }},
}


def _png(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (CARD_W, CARD_H), color).save(buf, "PNG")
    return buf.getvalue()


class _Resp:
    def __init__(self, body=None, content=b""):
        self._b, self.content, self.status_code = body, content, 200
        self.text = str(body)

    def json(self):
        return self._b


class _DemoTransport:
    """Deterministic, offline — returns ACE's documented response shapes."""

    def call(self, service, payload):
        if service == "serp":
            return _Resp({"organic": [
                {"title": "Jupiter aggregator volume climbs",
                 "link": "https://defillama.com/protocol/jupiter",
                 "snippet": "Jupiter routed a large multi-hop USDC swap; "
                            "aggregator volume up on the day."},
                {"title": "Solana DEX flow",
                 "link": "https://solscan.io/",
                 "snippet": "On-chain flow shows a sizable settled route "
                            "through the Jupiter program."},
            ]})
        if service == "chat":
            return _Resp({"choices": [{"message": {"role": "assistant",
                "content": (
                    "A multi-hop swap of roughly 1.28M USDC settled through "
                    "Jupiter in a single transaction. The route touched the "
                    "SPL token program and completed without error. Activity "
                    "like this marks meaningful aggregator flow rather than "
                    "routine noise.")}}]})
        if service == "image":
            return _Resp({"success": True, "task_id": "demo_tsk",
                          "data": [{"image_url": "https://cdn.demo/i.png"}]})
        raise AssertionError(service)

    def image_task(self, task_id):  # pragma: no cover - sync path used here
        return _Resp({"success": True, "state": "completed",
                      "data": [{"image_url": "https://cdn.demo/i.png"}]})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Offline OnchainBrief demo")
    ap.add_argument("--out", default="./demo-out",
                    help="output dir (default ./demo-out)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    out = pathlib.Path(args.out)
    briefs, site = out / "briefs", out / "site" / "index.html"

    ev = SolanaLogWatcher._parse(_SAMPLE_NOTIF)
    assert ev is not None, "sample event failed to parse"
    print(f"event   : sig={ev.signature[:24]}… programs={ev.program_ids}")
    print(f"filter  : brief-worthy = {is_brief_worthy(ev)}  "
          f"(heuristic — local LLM not required offline)")

    client = AceBriefClient(
        _DemoTransport(),
        image_fetch=lambda _u: _Resp(content=_png((14, 32, 58))),
    )
    art = run_brief(ev, client, briefs)
    build_feed(briefs, site)

    print(f"brief   : {art.headline}")
    print(f"card    : {art.card_path}")
    print(f"md      : {art.brief_path}")
    print(f"sources : {art.sources}")
    print(f"\nDONE — open the feed:\n  {site.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
