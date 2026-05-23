"""Standard credit-billed Ace Data Cloud client.

Handles Bearer-token calls that consume account credits for development
and testing.
"""

from __future__ import annotations

from typing import Any

import requests

from .config import (
    ACE_API_BASE,
    ACE_ENDPOINTS,
    ACE_IMAGE_TASKS_PATH,
    IMAGE_MODEL,
    IMAGE_SIZE,
    SERP_RESULT_COUNT,
    Settings,
)


class AceError(RuntimeError):
    pass


class AceClient:
    def __init__(self, settings: Settings | None = None, timeout: float = 120.0):
        self.settings = settings or Settings.load()
        self.settings.require("ace_api_token")
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update(
            {
                "accept": "application/json",
                "authorization": f"Bearer {self.settings.ace_api_token}",
                "content-type": "application/json",
            }
        )

    def call(self, service: str, payload: dict[str, Any]) -> requests.Response:
        """Uniform transport surface (mirrors X402Client.call)."""
        return self._post(ACE_ENDPOINTS[service], payload, service=service)

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        service: str | None = None,
    ) -> requests.Response:
        url = f"{ACE_API_BASE}{path}"
        # Image generation blocks until the image is ready when no
        # callback_url is set; that can exceed the default chat/serp timeout.
        timeout = max(self.timeout, 300.0) if path == ACE_ENDPOINTS["image"] else self.timeout
        # Image requires a per-Application credential — the account-wide
        # token returns "No available channel for model … under group
        # default". Image and the image task-polling endpoint both go through
        # the image credential; chat/serp keep using the account-wide token.
        headers: dict[str, str] | None = None
        if service == "image" or path == ACE_IMAGE_TASKS_PATH:
            headers = {
                "authorization": f"Bearer {self.settings.ace_api_token_image}",
            }
        resp = self._s.post(url, json=payload, timeout=timeout, headers=headers)
        if resp.status_code >= 400:
            raise AceError(f"{path} {resp.status_code}: {resp.text[:500]}")
        return resp

    # --- the three distinct services the pipeline needs ---

    def chat(self, prompt: str, model: str = "gpt-4o-mini") -> requests.Response:
        return self.call(
            "chat",
            {"model": model, "messages": [{"role": "user", "content": prompt}]},
        )

    def serp(self, query: str) -> requests.Response:
        return self.call("serp", {"query": query, "number": SERP_RESULT_COUNT})

    def image(self, prompt: str) -> requests.Response:
        # No callback_url => synchronous: the call blocks until the image is
        # generated and returns it directly (same shape as the older FluxMCP
        # core/client.py this contract was researched from).
        return self.call(
            "image",
            {
                "action": "generate",
                "model": IMAGE_MODEL,
                "prompt": prompt,
                "size": IMAGE_SIZE,
                "count": 1,
            },
        )

    def image_task(self, task_id: str) -> requests.Response:
        """Poll an image task when the image call returned async (task_id only)."""
        return self._post(ACE_IMAGE_TASKS_PATH, {"id": task_id})
