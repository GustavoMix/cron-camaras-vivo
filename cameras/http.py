"""Shared HTTP client.

Upstream feeds are public services run by transport agencies, so the client is
deliberately polite: a low concurrency ceiling, an identifying User-Agent, and
exponential backoff that respects ``Retry-After``.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import httpx

log = logging.getLogger(__name__)

USER_AGENT = (
    "cron-camaras-vivo/1.0 (+https://github.com/GustavoMix/cron-camaras-vivo) "
    "public-webcam-index"
)

DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)

# Status codes where retrying can plausibly succeed. A 404 or 401 will not fix
# itself, so those fail immediately and show up in the health report.
RETRY_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})


class FetchError(RuntimeError):
    """Raised when a URL could not be retrieved after all retries."""


class Http:
    """Thin wrapper over :class:`httpx.AsyncClient` with retries and a limiter."""

    def __init__(self, *, concurrency: int = 8, retries: int = 3) -> None:
        self._client = httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate",
            },
            # Cameras feeds are chatty; keep connections warm.
            limits=httpx.Limits(
                max_connections=concurrency * 2,
                max_keepalive_connections=concurrency,
            ),
        )
        self._semaphore = asyncio.Semaphore(concurrency)
        self._retries = retries

    async def __aenter__(self) -> Http:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """GET with retries. Raises :class:`FetchError` when every attempt fails."""
        last_error: str = "unknown error"

        for attempt in range(self._retries + 1):
            if attempt:
                await asyncio.sleep(self._backoff(attempt))
            try:
                async with self._semaphore:
                    response = await self._client.get(url, params=params, headers=headers)
            except (httpx.HTTPError, OSError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.debug("GET %s failed (attempt %d): %s", url, attempt + 1, last_error)
                continue

            if response.status_code in RETRY_STATUS:
                last_error = f"HTTP {response.status_code}"
                await self._honour_retry_after(response)
                continue
            if response.status_code >= 400:
                raise FetchError(f"HTTP {response.status_code} for {url}")
            return response

        raise FetchError(f"{last_error} for {url}")

    async def get_json(
        self,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        """GET and parse JSON, ignoring a wrong or missing Content-Type."""
        response = await self.get(url, params=params, headers=headers)
        try:
            return response.json()
        except ValueError as exc:
            snippet = response.text[:120].replace("\n", " ")
            raise FetchError(f"invalid JSON from {url}: {exc} (body starts: {snippet!r})") from exc

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with jitter, so parallel sources do not sync up."""
        return min(2.0 ** attempt, 12.0) + random.uniform(0.0, 0.75)

    async def _honour_retry_after(self, response: httpx.Response) -> None:
        raw = response.headers.get("Retry-After")
        if not raw:
            return
        try:
            delay = float(raw)
        except ValueError:
            return
        await asyncio.sleep(min(max(delay, 0.0), 30.0))
