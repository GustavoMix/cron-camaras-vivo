"""Verify that a camera's media is reachable, live and playable.

Two depths keep the cron cheap:

* **light** - manifest + one ranged request for the newest segment.
* **deep**  - light, plus a second manifest fetch after a short wait to prove
  the playlist is actually advancing (not a frozen or looping file). Used only
  for new or recovering cameras.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .hls import Playlist, parse_playlist, pick_variant, tizen_playable

MAX_IMAGE_BYTES = 1_500_000
MIN_IMAGE_BYTES = 4_096
MAX_IMAGE_AGE_S = 2 * 86_400
PLAYABLE_FORMATS = frozenset({"hls", "mp4", "dash"})


@dataclass(slots=True)
class ProbeResult:
    ok: bool
    media: str  # hls | mp4 | dash | image
    reason: str | None = None
    latency_ms: int = 0
    width: int | None = None
    height: int | None = None
    bandwidth: int | None = None
    codecs: str | None = None
    tizen_ok: bool | None = None
    age_s: int | None = None

    def to_json(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


class Prober:
    def __init__(
        self, client: httpx.AsyncClient, *, concurrency: int = 12, deep_wait_s: float = 4.0
    ) -> None:
        self._client = client
        self._sem = asyncio.Semaphore(concurrency)
        self._deep_wait_s = deep_wait_s

    async def probe(self, url: str, fmt: str | None, *, deep: bool = False) -> ProbeResult:
        media = fmt if fmt in PLAYABLE_FORMATS else "image"
        started = time.monotonic()
        try:
            async with self._sem:
                if media == "hls":
                    result = await self._hls(url, deep)
                elif media == "image":
                    result = await self._image(url)
                else:
                    result = await self._ranged(url, media)
        except httpx.TimeoutException:
            result = ProbeResult(False, media, "timeout")
        except (httpx.HTTPError, OSError) as exc:
            result = ProbeResult(False, media, f"net_{type(exc).__name__}")
        except ValueError as exc:
            result = ProbeResult(False, media, f"invalid:{exc}"[:60])
        result.latency_ms = int((time.monotonic() - started) * 1000)
        return result

    async def _hls(self, url: str, deep: bool) -> ProbeResult:
        response = await self._client.get(url)
        if response.status_code >= 400:
            return ProbeResult(False, "hls", f"http_{response.status_code}")
        playlist = parse_playlist(response.text, str(response.url))
        media_url = str(response.url)
        width = height = bandwidth = None
        codecs = None

        if playlist.is_master:
            variant = pick_variant(playlist.variants)
            if variant is None:
                return ProbeResult(False, "hls", "empty_master")
            width, height, bandwidth, codecs = (
                variant.width,
                variant.height,
                variant.bandwidth,
                variant.codecs,
            )
            response = await self._client.get(variant.url)
            if response.status_code >= 400:
                return ProbeResult(False, "hls", f"variant_http_{response.status_code}")
            playlist = parse_playlist(response.text, str(response.url))
            media_url = str(response.url)
            if playlist.is_master:
                return ProbeResult(False, "hls", "nested_master")

        verdict = _judge_media_playlist(playlist)
        if verdict:
            return ProbeResult(False, "hls", verdict, width=width, height=height)

        segment = await self._client.get(playlist.segments[-1], headers={"Range": "bytes=0-2047"})
        if segment.status_code not in (200, 206):
            return ProbeResult(False, "hls", f"segment_http_{segment.status_code}")
        if not segment.content:
            return ProbeResult(False, "hls", "segment_empty")

        if deep:
            wait = min(self._deep_wait_s, (playlist.target_duration or 4.0) * 1.2)
            await asyncio.sleep(wait)
            again = parse_playlist((await self._client.get(media_url)).text, media_url)
            if not _advanced(playlist, again):
                return ProbeResult(False, "hls", "stalled", width=width, height=height)

        return ProbeResult(
            True,
            "hls",
            width=width,
            height=height,
            bandwidth=bandwidth,
            codecs=codecs,
            tizen_ok=tizen_playable(codecs),
        )

    async def _ranged(self, url: str, media: str) -> ProbeResult:
        response = await self._client.get(url, headers={"Range": "bytes=0-4095"})
        if response.status_code not in (200, 206):
            return ProbeResult(False, media, f"http_{response.status_code}")
        if media == "dash" and b"<MPD" not in response.content[:2048]:
            return ProbeResult(False, media, "not_dash")
        if media == "mp4" and not response.content:
            return ProbeResult(False, media, "empty")
        return ProbeResult(True, media, tizen_ok=True)

    async def _image(self, url: str) -> ProbeResult:
        async with self._client.stream("GET", url) as response:
            if response.status_code >= 400:
                return ProbeResult(False, "image", f"http_{response.status_code}")
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > MAX_IMAGE_BYTES:
                    break
            last_modified = response.headers.get("Last-Modified")
        if len(body) < MIN_IMAGE_BYTES:
            return ProbeResult(False, "image", "too_small")
        if not _is_image(bytes(body[:12])):
            return ProbeResult(False, "image", "not_image")
        age = _age_seconds(last_modified)
        if age is not None and age > MAX_IMAGE_AGE_S:
            return ProbeResult(False, "image", "stale", age_s=age)
        return ProbeResult(True, "image", age_s=age, tizen_ok=True)


def _judge_media_playlist(playlist: Playlist) -> str | None:
    if playlist.drm:
        return "drm"
    if playlist.ended:
        return "not_live"
    if not playlist.segments:
        return "no_segments"
    return None


def _advanced(before: Playlist, after: Playlist) -> bool:
    if before.media_sequence is not None and after.media_sequence is not None:
        return after.media_sequence > before.media_sequence
    return bool(after.segments) and after.segments[-1] != before.segments[-1]


def _is_image(head: bytes) -> bool:
    return head.startswith((b"\xff\xd8\xff", b"\x89PNG")) or (
        head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    )


def _age_seconds(header: str | None) -> int | None:
    if not header:
        return None
    try:
        moment = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - moment).total_seconds()))
