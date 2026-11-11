"""Minimal HLS playlist parser: just what is needed to judge a live stream."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


@dataclass(slots=True)
class Variant:
    url: str
    bandwidth: int = 0
    width: int | None = None
    height: int | None = None
    codecs: str | None = None


@dataclass(slots=True)
class Playlist:
    is_master: bool = False
    variants: list[Variant] = field(default_factory=list)
    segments: list[str] = field(default_factory=list)
    target_duration: float | None = None
    media_sequence: int | None = None
    ended: bool = False
    drm: bool = False


def _attrs(line: str) -> dict[str, str]:
    body = line.split(":", 1)[1] if ":" in line else ""
    return {key: value.strip('"') for key, value in _ATTR.findall(body)}


def parse_playlist(text: str, base_url: str) -> Playlist:
    """Parse a master or media playlist. Raises ``ValueError`` if not HLS."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines or not lines[0].startswith("#EXTM3U"):
        raise ValueError("missing #EXTM3U header")

    playlist = Playlist()
    pending: dict[str, str] | None = None

    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF"):
            pending = _attrs(line)
            playlist.is_master = True
        elif line.startswith("#EXT-X-TARGETDURATION"):
            playlist.target_duration = _to_float(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE"):
            playlist.media_sequence = _to_int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-ENDLIST") or (
            line.startswith("#EXT-X-PLAYLIST-TYPE") and "VOD" in line
        ):
            playlist.ended = True
        elif line.startswith("#EXT-X-KEY"):
            attrs = _attrs(line)
            method = attrs.get("METHOD", "NONE").upper()
            keyformat = attrs.get("KEYFORMAT", "identity").lower()
            if method == "SAMPLE-AES" or keyformat != "identity":
                playlist.drm = True
        elif line.startswith("#"):
            continue
        elif pending is not None:
            width = height = None
            resolution = pending.get("RESOLUTION", "")
            if "x" in resolution:
                w, _, h = resolution.partition("x")
                width, height = _to_int(w), _to_int(h)
            playlist.variants.append(
                Variant(
                    url=urljoin(base_url, line),
                    bandwidth=_to_int(pending.get("BANDWIDTH", "0")) or 0,
                    width=width,
                    height=height,
                    codecs=pending.get("CODECS"),
                )
            )
            pending = None
        else:
            playlist.segments.append(urljoin(base_url, line))

    return playlist


def pick_variant(variants: list[Variant], max_height: int = 1080) -> Variant | None:
    """Best variant a TV would pick: highest bandwidth not above ``max_height``."""
    if not variants:
        return None
    fitting = [v for v in variants if v.height is None or v.height <= max_height]
    if fitting:
        return max(fitting, key=lambda v: (v.bandwidth, v.height or 0))
    return min(variants, key=lambda v: v.height or 10_000)


def tizen_playable(codecs: str | None) -> bool:
    """True when every codec is one that all Tizen TVs since 2015 can decode.

    HEVC, VP9 and AV1 depend on the model year, so they are not counted as
    safe: the catalog should work on the oldest TV we claim to support.
    """
    if not codecs:
        return True  # unknown: optimistic; the segment probe still has to pass
    safe = ("avc1", "avc3", "mp4a", "mp3", "ac-3", "ec-3")
    return all(part.strip().lower().startswith(safe) for part in codecs.split(","))


def _to_int(value: str) -> int | None:
    try:
        return int(float(value.strip()))
    except ValueError:
        return None


def _to_float(value: str) -> float | None:
    try:
        return float(value.strip())
    except ValueError:
        return None
