"""Entrypoint: watcher -> local filter -> daily throttle -> brief pipeline."""

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

ON_BRIEF_COMPILED_CALLBACK = None


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
    except Exception as e:  # noqa: BLE001 - keep agent alive on bad keypair
        print(f"[ATTEST] disabled (keypair load failed): {e!r}")
        return None


async def _log_only_pipeline(ev: LogEvent) -> None:
    print(f"[BRIEF-CANDIDATE] sig={ev.signature} programs={ev.program_ids} "
          f"(no ACE creds - log-only, not spending. Set ACE_API_TOKEN "
          f"[+ACE_X402_PRIVATE_KEY for the x402 bar] to go live.)")


def _build_transport():
    """Return (transport, mode) or (None, 'log-only') if no creds."""
    if os.getenv("ACE_X402_PRIVATE_KEY"):
        from .x402_client import X402Client

        return X402Client(), "x402 (on-chain settled)"
    if os.getenv("ACE_API_TOKEN"):
        from .ace_client import AceClient

        return AceClient(), "credit (Bearer token - dev mode)"
    return None, "log-only"


def make_real_pipeline(skip_image: bool = False):
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
    print(f"[PIPELINE] live: {mode} | {attest_mode} -> {BRIEFS_DIR} -> {SITE_HTML} (skip_image={skip_image})")

    async def _pipeline(ev: LogEvent) -> None:
        art = await run_brief_async(ev, client, BRIEFS_DIR, attestor=attestor, skip_image=skip_image)
        await asyncio.to_thread(build_feed, BRIEFS_DIR, SITE_HTML)
        receipt = (
            f" attest={art.attestation.tx_sig[:16]}…" if art.attestation else ""
        )
        card_dest = art.card_path if art.card_path else "no-image"
        print(f"[BRIEF] {art.headline} -> {card_dest}{receipt} | feed {SITE_HTML}")
        if ON_BRIEF_COMPILED_CALLBACK is not None:
            try:
                ON_BRIEF_COMPILED_CALLBACK(art.signature)
            except Exception as e:
                print(f"[CALLBACK-ERROR] {e!r}")

    return _pipeline


async def handle(
    ev: LogEvent, pipeline, *, once: bool, watcher=None, spends: bool = True
) -> None:
    # Run the blocking filter inside a worker thread so the async loop stays free
    worthy = await asyncio.to_thread(is_brief_worthy, ev)
    if not worthy:
        return
    # Decode the transaction into real facts (amount/asset/parties/program)
    # before spending on the brief - this is what makes the brief say what
    # happened instead of paraphrasing a SERP of the program id. Best-effort:
    # a failed RPC round-trip leaves ev.facts None and the pipeline falls back.
    rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    # getTransaction goes to a decode RPC (OOBE's free tier can't serve it).
    tx_rpc_url = os.getenv("SOLANA_TX_RPC_URL", "https://api.mainnet.solana.com")
    try:
        from .txfacts import enrich_event

        await asyncio.to_thread(enrich_event, ev, rpc_url, tx_rpc_url=tx_rpc_url)
    except Exception as e:  # noqa: BLE001 - enrichment is non-critical
        print(f"[ENRICH-SKIP] {ev.signature}: {e!r}")
    # Hard editorial bar: the cheap pre-enrich filter only knew "something
    # non-trivial happened"; now that the tx is decoded we can reject the events
    # that are technically a swap/transfer but not worth a public brief (a sub-
    # threshold flow, or undecodable activity). Spending below the bar is what
    # filled the feed with $0.11 cards.
    from .filter import is_significant

    if not is_significant(ev):
        facts = getattr(ev, "facts", None)
        kind = getattr(facts, "kind", "?")
        usd = getattr(facts, "amount_usd", None)
        print(f"[SKIP-INSIGNIFICANT] {ev.signature} kind={kind} usd={usd}")
        return
    # The throttle is a spend gate; log-only mode never spends, so it must
    # not burn the daily budget (else a no-creds dry-run logs one candidate
    # then prints [THROTTLED] forever at the default cap of 1).
    if spends and not throttle.try_consume():
        print(f"[THROTTLED] daily cap reached, skipping {ev.signature}")
        return
    try:
        await pipeline(ev)
    except Exception as e:  # noqa: BLE001 - resilience boundary
        # One failed ACE call (bad token, API error, CDN/network blip) must
        # NOT kill the long-running watcher. Log and survive. The throttle
        # slot stays consumed: the call may have already cost a credit, and
        # the low-spend lane prefers under-spending to a refund race.
        print(f"[BRIEF-ERROR] {ev.signature}: {e!r}")
    finally:
        if once and watcher is not None:
            watcher.stop()


def build_watcher(pipeline=None, *, once: bool = False, skip_image: bool = False) -> SolanaLogWatcher:
    if pipeline is None:
        real = make_real_pipeline(skip_image=skip_image)
        spends = real is not None  # log-only fallback must not gate on spend
        pipeline = real or _log_only_pipeline
    else:
        spends = True  # an explicitly injected pipeline is assumed to spend
    ws_url = os.getenv("SOLANA_WS_URL", "wss://us-1-mainnet.oobeprotocol.ai/ws")
    program_ids = [
        p.strip()
        for p in os.getenv("WATCH_PROGRAM_IDS", "").split(",")
        if p.strip()
    ]
    if not program_ids:
        raise SystemExit(
            "Set WATCH_PROGRAM_IDS (comma-separated) - the curated programs to "
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
    skip_image = "--no-image" in args or "--skip-image" in args or os.getenv("SKIP_IMAGE", "").lower() in ("true", "1", "yes")
    asyncio.run(build_watcher(once=once, skip_image=skip_image).run())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
