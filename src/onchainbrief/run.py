"""Entrypoint: watcher -> local filter -> daily throttle -> brief pipeline.

The paid ACE pipeline (SERP -> chat -> image, settled via x402) is
injected so this orchestration is testable without funds.

Pipeline selection (safe by default):
  * ACE_X402_PRIVATE_KEY set  -> X402Client transport  (every ACE
                                  call settled on-chain — Bearer would
                                  disable 402, so x402 mode must NOT
                                  send ACE_API_TOKEN)
  * only ACE_API_TOKEN set    -> AceClient transport    (credit dev mode)
  * neither                   -> log-only, NO spend (default)

Each produced brief is written to BRIEFS_DIR and the static feed
(SITE_HTML) is rebuilt so the public site stays current.
"""

from __future__ import annotations

import asyncio
import os
import sys

from . import throttle
from .feed import build_feed
from .filter import is_brief_worthy
from .pipeline import run_brief_async
from .watcher import LogEvent, SolanaLogWatcher

BRIEFS_DIR = os.getenv("BRIEFS_DIR", "./briefs")
SITE_HTML = os.getenv("SITE_HTML", "./site/index.html")


def _build_attestor():
    """Build the on-chain attestor when a keypair path is set; else None.

    Safe default: no keypair = no attestation, agent still runs and ships
    briefs without the second anchor. ATTEST_CLUSTER (devnet by default)
    decides which Solana cluster the memo lands on.
    """
    kp = os.getenv("SOLANA_KEYPAIR_PATH", "")
    if not kp:
        return None
    cluster = os.getenv("ATTEST_CLUSTER", "devnet")
    rpc = os.getenv(
        "ATTEST_RPC_URL",
        "https://api.devnet.solana.com"
        if cluster == "devnet"
        else "https://api.mainnet-beta.solana.com",
    )
    from .attest import Attestor

    try:
        return Attestor(rpc, kp, cluster=cluster)
    except Exception as e:  # noqa: BLE001 — keep agent alive on bad keypair
        print(f"[ATTEST] disabled (keypair load failed): {e!r}")
        return None


async def _log_only_pipeline(ev: LogEvent) -> None:
    print(f"[BRIEF-CANDIDATE] sig={ev.signature} programs={ev.program_ids} "
          f"(no ACE creds — log-only, not spending. Set ACE_API_TOKEN "
          f"[+ACE_X402_PRIVATE_KEY for the x402 bar] to go live.)")


def _build_transport():
    """Return (transport, mode) or (None, 'log-only') if no creds."""
    if os.getenv("ACE_X402_PRIVATE_KEY"):
        from .x402_client import X402Client

        return X402Client(), "x402 (on-chain settled)"
    if os.getenv("ACE_API_TOKEN"):
        from .ace_client import AceClient

        return AceClient(), "credit (Bearer token — dev mode)"
    return None, "log-only"


def make_real_pipeline():
    """Build the funded pipeline closure, or None if creds are absent."""
    transport, mode = _build_transport()
    if transport is None:
        return None
    from .ace_brief_client import AceBriefClient

    client = AceBriefClient(transport)
    attestor = _build_attestor()
    attest_mode = (
        f"attest:{attestor.cluster}" if attestor is not None else "attest:off"
    )
    print(f"[PIPELINE] live: {mode} | {attest_mode} -> {BRIEFS_DIR} -> {SITE_HTML}")

    async def _pipeline(ev: LogEvent) -> None:
        art = await run_brief_async(ev, client, BRIEFS_DIR, attestor=attestor)
        await asyncio.to_thread(build_feed, BRIEFS_DIR, SITE_HTML)
        receipt = (
            f" attest={art.attestation.tx_sig[:16]}…" if art.attestation else ""
        )
        print(f"[BRIEF] {art.headline} -> {art.card_path}{receipt} | feed {SITE_HTML}")

    return _pipeline


async def handle(
    ev: LogEvent, pipeline, *, once: bool, watcher=None, spends: bool = True
) -> None:
    # Run the blocking filter inside a worker thread so the async loop stays free
    worthy = await asyncio.to_thread(is_brief_worthy, ev)
    if not worthy:
        return
    # The throttle is a *spend* gate; log-only mode never spends, so it must
    # not burn the daily budget (else a no-creds dry-run logs one candidate
    # then prints [THROTTLED] forever at the default cap of 1).
    if spends and not throttle.try_consume():
        print(f"[THROTTLED] daily cap reached, skipping {ev.signature}")
        return
    try:
        await pipeline(ev)
    except Exception as e:  # noqa: BLE001 — resilience boundary
        # One failed ACE call (bad token, API error, CDN/network blip) must
        # NOT kill the long-running watcher. Log and survive. The throttle
        # slot stays consumed: the call may have already cost a credit, and
        # the low-spend lane prefers under-spending to a refund race.
        print(f"[BRIEF-ERROR] {ev.signature}: {e!r}")
    finally:
        if once and watcher is not None:
            watcher.stop()


def build_watcher(pipeline=None, *, once: bool = False) -> SolanaLogWatcher:
    if pipeline is None:
        real = make_real_pipeline()
        spends = real is not None  # log-only fallback must not gate on spend
        pipeline = real or _log_only_pipeline
    else:
        spends = True  # an explicitly injected pipeline is assumed to spend
    ws_url = os.getenv("SOLANA_WS_URL", "wss://api.mainnet-beta.solana.com")
    program_ids = [
        p.strip()
        for p in os.getenv("WATCH_PROGRAM_IDS", "").split(",")
        if p.strip()
    ]
    if not program_ids:
        raise SystemExit(
            "Set WATCH_PROGRAM_IDS (comma-separated) — the curated programs to "
            "watch. Empty by design: low-spend wants a deliberate scope."
        )
    watcher = SolanaLogWatcher(ws_url, program_ids)
    watcher.on_event = lambda ev: handle(
        ev, pipeline, once=once, watcher=watcher, spends=spends
    )
    return watcher


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    once = "--once" in args
    asyncio.run(build_watcher(once=once).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
