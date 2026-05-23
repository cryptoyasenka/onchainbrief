"""AceBriefClient — the real BriefClient over ACE's 3 distinct services.

This is the glue between the raw transports (AceClient = credit/Bearer, or
X402Client = on-chain x402 settlement) and the pipeline's
`BriefClient` protocol (serp -> SerpResult, chat -> str, image -> bytes).

Response parsing is **doc-derived**, not assumed: shapes come from the
official AceDataCloud repos —

  * serp  : SerpAPI docs/google_serp_api_integration_guide.md
            -> POST /serp/google {"query":...} -> {"organic":[{title,link,snippet}]}
  * chat  : OpenAIAPI README
            -> POST /openai/chat/completions -> choices[0].message.content
  * image : NanoBanana (Gemini wrapper) — same shape as the older FluxAPI
            from which this contract was originally doc-derived.
            -> POST /nano-banana/images {"action":"generate",...}
            -> {"success":true,"task_id":...,"data":[{"image_url":...}]}
            (blocks until ready with no callback_url; else poll /nano-banana/tasks)

Parsing is deliberately *defensive* (tries the documented key, then known
aliases) and every extractor is marked so the exact live wrapper is
re-confirmed empirically at the first funded call without a code
change. Errors that arrive HTTP-200 as {"error":{...}} or success=false are
raised, not silently turned into empty briefs.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Protocol

import requests

from .config import IMAGE_MODEL, IMAGE_SIZE, SERP_RESULT_COUNT
from .pipeline import SerpResult


class AceBriefError(RuntimeError):
    pass


class Transport(Protocol):  # structural; AceClient and X402Client satisfy it
    def call(self, service: str, payload: dict[str, Any]) -> requests.Response: ...
    def image_task(self, task_id: str) -> requests.Response: ...


def _json(resp: requests.Response, what: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError as e:
        raise AceBriefError(f"{what}: non-JSON response: {resp.text[:300]}") from e
    if isinstance(body, dict):
        # ACE returns 200 with {"error":{"code","message"}} or success=false
        if body.get("success") is False or "error" in body:
            err = body.get("error") or body
            msg = err.get("message") if isinstance(err, dict) else err
            raise AceBriefError(f"{what}: API error: {msg}")
    return body if isinstance(body, dict) else {"_list": body}


def _first(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


class AceBriefClient:
    """Implements pipeline.BriefClient over an ACE transport.

    transport    : AceClient (credit) or X402Client (on-chain x402)
    image_fetch  : how to download the generated image bytes (injectable
                   for tests; the CDN GET is not an ACE-billable call)
    sleep        : injectable so the image async-poll path is testable
    """

    def __init__(
        self,
        transport: Transport,
        *,
        image_fetch: Callable[[str], requests.Response] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        chat_model: str = "gpt-4o-mini",
        poll_interval: float = 15.0,
        poll_attempts: int = 40,
    ):
        self._t = transport
        self._fetch = image_fetch or (lambda u: requests.get(u, timeout=120))
        self._sleep = sleep
        self._chat_model = chat_model
        self._poll_interval = poll_interval
        self._poll_attempts = poll_attempts

    # --- service 1: SERP -> context summary + source urls ---

    def serp(self, query: str) -> SerpResult:
        body = _json(
            self._t.call("serp", {"query": query, "number": SERP_RESULT_COUNT}),
            "serp",
        )
        organic = (
            # "_list" = a bare JSON array body, wrapped by _json(); without
            # this the defensive wrap was unreachable dead code.
            _first(body, "organic", "organic_results", "results", "_list")
            or _first(body.get("data", {}) if isinstance(body.get("data"), dict) else {},
                      "organic", "results")
            or (body.get("data") if isinstance(body.get("data"), list) else None)
            or []
        )
        # Guarantee a list before slicing/iterating — a malformed dict here
        # would otherwise raise, contradicting the "defensive" contract.
        if isinstance(organic, dict):
            organic = _first(organic, "organic", "results") or list(
                organic.values()
            )
        if not isinstance(organic, list):
            organic = []
        snippets: list[str] = []
        sources: list[str] = []
        for r in organic[:SERP_RESULT_COUNT]:
            if not isinstance(r, dict):
                continue
            snip = _first(r, "snippet", "summary", "description", "title")
            link = _first(r, "link", "url", "website", "displayed_link")
            if snip:
                snippets.append(str(snip).strip())
            if link and str(link).startswith("http"):
                sources.append(str(link))
        summary = " ".join(snippets)[:2000] or f"No web context found for: {query}"
        # de-dupe sources, keep order
        seen: set[str] = set()
        uniq = [s for s in sources if not (s in seen or seen.add(s))]
        return SerpResult(summary=summary, sources=uniq[:SERP_RESULT_COUNT])

    # --- service 2: chat -> narrative text ---

    def chat(self, prompt: str) -> str:
        body = _json(
            self._t.call(
                "chat",
                {
                    "model": self._chat_model,
                    "messages": [{"role": "user", "content": prompt}],
                },
            ),
            "chat",
        )
        choices = _first(body, "choices") or _first(
            body.get("data", {}) if isinstance(body.get("data"), dict) else {},
            "choices",
        )
        if isinstance(choices, list) and choices:
            c0 = choices[0]
            if isinstance(c0, dict):
                msg = c0.get("message") or {}
                text = (
                    (msg.get("content") if isinstance(msg, dict) else None)
                    or c0.get("text")
                    or c0.get("content")
                )
                if text:
                    return str(text).strip()
        # last-resort documented aliases
        text = _first(body, "content", "text", "output")
        if text:
            return str(text).strip()
        raise AceBriefError(f"chat: no assistant text in response: {body}")

    # --- service 3: image (nano-banana) -> raw PNG/JPEG bytes ---

    def image(self, prompt: str) -> bytes:
        body = _json(
            self._t.call(
                "image",
                {
                    "action": "generate",
                    "model": IMAGE_MODEL,
                    "prompt": prompt,
                    "size": IMAGE_SIZE,
                    "count": 1,
                },
            ),
            "image",
        )
        url = self._image_url(body)
        if not url:
            task_id = _first(body, "task_id", "id", "taskId")
            if not task_id:
                raise AceBriefError(f"image: no image_url and no task_id: {body}")
            url = self._poll_image(str(task_id))
        img = self._fetch(url)
        if getattr(img, "status_code", 200) >= 400:
            raise AceBriefError(f"image: download {img.status_code} for {url}")
        return img.content

    def _poll_image(self, task_id: str) -> str:
        for attempt in range(self._poll_attempts):
            self._sleep(self._poll_interval)
            try:
                resp = self._t.image_task(task_id)
                body = _json(resp, "image_task")
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    f"Transient failure polling image task {task_id} on attempt {attempt + 1}: {e!r}"
                )
                continue

            url = self._image_url(body)
            if url:
                return url
            state = str(_first(body, "state", "status") or "").lower()
            if state in ("failed", "error", "canceled", "cancelled"):
                raise AceBriefError(f"image: task {task_id} {state}: {body}")
        raise AceBriefError(
            f"image: task {task_id} not ready after "
            f"{self._poll_attempts} polls"
        )

    @staticmethod
    def _image_url(body: dict[str, Any]) -> str | None:
        data = body.get("data")
        if isinstance(data, list) and data and isinstance(data[0], dict):
            u = _first(data[0], "image_url", "url", "image")
            if u:
                return str(u)
        if isinstance(data, dict):
            u = _first(data, "image_url", "url", "image")
            if u:
                return str(u)
        urls = _first(body, "image_urls", "images")
        if isinstance(urls, list) and urls:
            return str(urls[0])
        u = _first(body, "image_url", "url")
        return str(u) if u else None
