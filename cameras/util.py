"""Small helpers shared by the source adapters.

Every adapter parses third-party JSON whose exact shape we do not control, so
the helpers here are all forgiving: they take whatever they are given and
return either a clean value or ``None``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit, urlunsplit

_WS = re.compile(r"\s+")
# Cache-busting parameters that some feeds append to snapshot URLs. They change
# on every poll, so they must not take part in identity or de-duplication.
_CACHE_BUSTERS = frozenset(
    {"t", "ts", "time", "timestamp", "rand", "random", "nocache", "_", "cb", "v"}
)


def first_of(data: Any, *keys: str) -> Any:
    """Return the first present, non-empty value among ``keys``.

    Lookup is case-insensitive because the same platform is deployed by
    different agencies with different casing conventions (``Latitude`` vs
    ``latitude`` vs ``lat``).
    """
    if not isinstance(data, dict):
        return None
    lowered = {str(k).lower(): v for k, v in data.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value not in (None, "", [], {}):
            return value
    return None


def clean_text(value: Any, *, limit: int = 200) -> str | None:
    """Collapse whitespace and strip control characters from a label."""
    if value is None:
        return None
    text = str(value).replace("\x00", " ")
    text = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    text = _WS.sub(" ", text).strip(" \t\r\n-–—|,;")
    if not text:
        return None
    return text[:limit].strip()


def coerce_float(value: Any) -> float | None:
    """Parse a float from a number or a string, tolerating stray formatting."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        text = value.strip().replace(",", ".")
        if not text:
            return None
        try:
            result = float(text)
        except ValueError:
            return None
    else:
        return None
    # NaN and the infinities are not representable in JSON.
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def valid_coords(lat: float | None, lon: float | None) -> bool:
    """Reject out-of-range pairs and the null island placeholder."""
    if lat is None or lon is None:
        return False
    if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
        return False
    return not (abs(lat) < 1e-6 and abs(lon) < 1e-6)


def normalize_url(value: Any, *, base_scheme: str = "https") -> str | None:
    """Return an absolute http(s) URL, or ``None`` if the value is unusable.

    Protocol-relative URLs are resolved against ``base_scheme`` and streaming
    schemes (``rtsp``/``rtmp``) are preserved, since some agencies publish only
    those.
    """
    if value is None:
        return None
    url = str(value).strip().replace(" ", "%20")
    if not url or url.lower() in {"null", "none", "n/a", "-"}:
        return None
    if url.startswith("//"):
        url = f"{base_scheme}:{url}"
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https", "rtsp", "rtmp"} or not parts.netloc:
        return None
    return urlunsplit(parts)


def stable_url_key(url: str | None) -> str | None:
    """Identity key for a media URL, ignoring cache-busting query parameters."""
    if not url:
        return None
    parts = urlsplit(url)
    kept = [
        pair
        for pair in parts.query.split("&")
        if pair and pair.split("=", 1)[0].lower() not in _CACHE_BUSTERS
    ]
    host = parts.netloc.lower()
    path = parts.path.rstrip("/") or "/"
    return f"{host}{path}?{'&'.join(sorted(kept))}"


def guess_stream_format(url: str | None) -> str | None:
    """Infer a playback format from the URL, for clients picking a player."""
    if not url:
        return None
    path = urlsplit(url).path.lower()
    scheme = urlsplit(url).scheme.lower()
    if scheme in {"rtsp", "rtmp"}:
        return scheme
    if path.endswith(".m3u8"):
        return "hls"
    if path.endswith(".mpd"):
        return "dash"
    if path.endswith((".mp4", ".m4v")):
        return "mp4"
    if path.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
        return "image"
    if "mjpg" in path or "mjpeg" in path:
        return "mjpeg"
    return None


def slugify(value: str, *, limit: int = 60) -> str:
    """ASCII slug used for human-readable parts of identifiers."""
    text = unicodedata.normalize("NFKD", str(value))
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:limit].strip("-")


def dedupe_key_name(name: str | None) -> str:
    """Aggressively normalized name, used only for near-duplicate detection."""
    if not name:
        return ""
    text = unicodedata.normalize("NFKD", name)
    text = text.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    """Yield ``items`` in lists of at most ``size`` elements."""
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
