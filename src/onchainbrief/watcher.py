"""Solana mainnet log watcher.

A single WS subscription with explicit sub-id tracking,
exponential-backoff reconnect, RPC health awareness, and secret redaction
in logs. OnchainBrief tails program logs (logsSubscribe) for a curated
program set and lets a pluggable filter decide which event is worth a
brief.

Low-spend by design: this only emits candidate events. The cost-bearing ACE
pipeline runs downstream and is throttled separately (see throttle.py).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import websockets

INITIAL_BACKOFF_S = 1.0
MAX_BACKOFF_S = 60.0
_SECRET_IN_URL = re.compile(r"(api[-_]?key=)[^&]+|/v2/[^/]+", re.IGNORECASE)


def redact(url: str) -> str:
    """Helius/QuickNode put the key in the URL — never log it raw."""
    return _SECRET_IN_URL.sub(lambda m: (m.group(1) or "") + "***", url)


def _next_backoff(cur: float) -> float:
    return min(cur * 2.0, MAX_BACKOFF_S)


@dataclass
class LogEvent:
    signature: str
    logs: list[str]
    program_ids: list[str]
    slot: int | None = None


EventHandler = Callable[[LogEvent], Awaitable[None]]


class SolanaLogWatcher:
    """Tails `logsSubscribe` for the given program IDs, reconnecting forever."""

    def __init__(
        self,
        ws_url: str,
        program_ids: list[str],
        on_event: EventHandler | None = None,
        commitment: str = "confirmed",
    ):
        if not program_ids:
            raise ValueError("watcher needs at least one program id to subscribe")
        self._ws_url = ws_url
        self._program_ids = program_ids
        # Public + optionally set after construction, so a handler that needs
        # to reference the watcher itself (e.g. --once calling .stop()) can be
        # attached without a forward-reference box.
        self.on_event = on_event
        self._commitment = commitment
        self._stopping = False

    def stop(self) -> None:
        self._stopping = True

    def _subscribe_frames(self) -> list[str]:
        # One logsSubscribe per program (mentions filter), tracked by req id.
        frames = []
        for i, pid in enumerate(self._program_ids, start=1):
            frames.append(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": i,
                        "method": "logsSubscribe",
                        "params": [
                            {"mentions": [pid]},
                            {"commitment": self._commitment},
                        ],
                    }
                )
            )
        return frames

    @staticmethod
    def _parse(msg: dict) -> LogEvent | None:
        if msg.get("method") != "logsNotification":
            return None
        res = msg.get("params", {}).get("result", {})
        val = res.get("value", {})
        sig = val.get("signature")
        if not sig or val.get("err") is not None:
            return None
        logs = val.get("logs", []) or []
        prog = []
        for ln in logs:
            if ln.startswith("Program ") and " invoke " in ln:
                parts = ln.split()
                if len(parts) >= 2:
                    prog.append(parts[1])
        return LogEvent(
            signature=sig,
            logs=logs,
            program_ids=sorted(set(prog)),
            slot=res.get("context", {}).get("slot"),
        )

    async def run(self) -> None:
        if self.on_event is None:
            raise RuntimeError("SolanaLogWatcher.on_event is not set")
        backoff = INITIAL_BACKOFF_S
        while not self._stopping:
            try:
                async with websockets.connect(
                    self._ws_url, ping_interval=20, ping_timeout=20
                ) as ws:
                    # Send subscription requests
                    sub_ids = {}
                    pending_ids = set(range(1, len(self._program_ids) + 1))
                    for frame in self._subscribe_frames():
                        await ws.send(frame)

                    # Wait for all confirmations
                    while pending_ids:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
                            msg = json.loads(raw)
                        except (asyncio.TimeoutError, ValueError, TypeError) as e:
                            raise websockets.WebSocketException(f"Subscription handshake failed/timeout: {e}")
                        
                        msg_id = msg.get("id")
                        if msg_id in pending_ids:
                            pending_ids.remove(msg_id)
                            if "error" in msg:
                                raise websockets.WebSocketException(
                                    f"Subscription rejected for req {msg_id}: {msg['error']}"
                                )
                            sub_ids[msg_id] = msg.get("result")

                    print(f"watcher connected and verified subscriptions: {redact(self._ws_url)}")
                    backoff = INITIAL_BACKOFF_S
                    async for raw in ws:
                        if self._stopping:
                            break
                        try:
                            msg = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        ev = self._parse(msg)
                        if ev is not None:
                            await self.on_event(ev)
                        # The handler may have called stop() (e.g. --once).
                        # Re-check now instead of blocking on the next frame —
                        # otherwise --once hangs until another log arrives.
                        if self._stopping:
                            break
            except (OSError, websockets.WebSocketException) as e:
                if self._stopping:
                    break
                print(f"watcher reconnect ({e!s}), backoff={backoff:.0f}s")
                await asyncio.sleep(backoff)
                backoff = _next_backoff(backoff)
