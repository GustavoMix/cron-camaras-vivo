"""The public data model: one normalized record per camera.

The JSON produced from :class:`Camera` is the contract consumed by client
applications, so field names here are effectively an API. Adding fields is
safe; renaming or removing them is a breaking change and needs a new
``SCHEMA_VERSION``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from .util import (
    clean_text,
    coerce_float,
    dedupe_key_name,
    guess_stream_format,
    normalize_url,
    stable_url_key,
    valid_coords,
)

SCHEMA_VERSION = 1

# Broad category of what the camera is pointed at.
KINDS = frozenset({"traffic", "scenic", "weather", "airport", "port", "other"})


@dataclass(slots=True)
class Camera:
    """A single camera, normalized across every upstream feed."""

    source: str
    native_id: str
    name: str
    country: str | None = None
    region: str | None = None
    city: str | None = None
    lat: float | None = None
    lon: float | None = None
    kind: str = "other"
    # Still snapshot that the publisher refreshes in place.
    image: str | None = None
    # Continuous stream (HLS/DASH/MJPEG/...).
    stream: str | None = None
    stream_format: str | None = None
    attribution: str | None = None
    license: str | None = None
    source_url: str | None = None
    tags: list[str] = field(default_factory=list)

    @property
    def id(self) -> str:
        """Stable identifier, derived from the source and its native key.

        Stability matters: clients cache by id, and the weekly diff of the
        committed dataset is only readable if ids do not churn between runs.
        """
        digest = hashlib.sha1(
            f"{self.source}\x00{self.native_id}".encode()
        ).hexdigest()
        return digest[:12]

    @property
    def has_media(self) -> bool:
        return bool(self.image or self.stream)

    def dedupe_keys(self) -> list[str]:
        """Keys that, if shared with another camera, mean it is the same one.

        Two independent aggregators frequently republish the same agency feed,
        so an identical media URL is the strongest signal. Position plus name
        catches the case where the same camera is served through two different
        CDN hostnames.
        """
        keys: list[str] = []
        for url in (self.stream, self.image):
            url_key = stable_url_key(url)
            if url_key:
                keys.append(f"url:{url_key}")
        name_key = dedupe_key_name(self.name)
        if valid_coords(self.lat, self.lon) and name_key:
            # ~11 m of precision: tight enough not to merge distinct cameras on
            # the same interchange, loose enough to absorb rounding differences.
            keys.append(f"geo:{self.lat:.4f},{self.lon:.4f}:{name_key[:40]}")
        return keys

    def to_json(self) -> dict[str, Any]:
        """Serialize, omitting empty fields to keep the payload small."""
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "source": self.source,
        }
        if self.country:
            payload["country"] = self.country
        if self.region:
            payload["region"] = self.region
        if self.city:
            payload["city"] = self.city
        if valid_coords(self.lat, self.lon):
            # 5 decimals is ~1 m, well beyond what these feeds actually know.
            payload["lat"] = round(float(self.lat), 5)
            payload["lon"] = round(float(self.lon), 5)
        payload["kind"] = self.kind
        if self.image:
            payload["image"] = self.image
        if self.stream:
            payload["stream"] = self.stream
            if self.stream_format:
                payload["stream_format"] = self.stream_format
        if self.tags:
            payload["tags"] = sorted(set(self.tags))
        if self.source_url:
            payload["source_url"] = self.source_url
        if self.attribution:
            payload["attribution"] = self.attribution
        if self.license:
            payload["license"] = self.license
        return payload


def make_camera(
    *,
    source: str,
    native_id: Any,
    name: Any,
    country: str | None = None,
    region: str | None = None,
    city: Any = None,
    lat: Any = None,
    lon: Any = None,
    kind: str = "other",
    image: Any = None,
    stream: Any = None,
    attribution: str | None = None,
    license: str | None = None,
    source_url: str | None = None,
    tags: list[str] | None = None,
) -> Camera | None:
    """Build a validated :class:`Camera`, or ``None`` if the record is unusable.

    Adapters funnel every record through here so that validation rules live in
    one place rather than being re-implemented per feed.
    """
    native = clean_text(native_id, limit=120)
    if not native:
        return None

    image_url = normalize_url(image)
    stream_url = normalize_url(stream)

    # A camera with no viewable media is just a map pin; drop it.
    if not image_url and not stream_url:
        return None

    # Some feeds put the snapshot in the stream field and vice versa. Trust the
    # URL over the field name.
    if stream_url and guess_stream_format(stream_url) == "image" and not image_url:
        image_url, stream_url = stream_url, None
    if (
        image_url
        and not stream_url
        and guess_stream_format(image_url) in {"hls", "dash", "mp4"}
    ):
        image_url, stream_url = None, image_url

    label = clean_text(name)
    if not label:
        # Never invent a name; fall back to the upstream identifier so the
        # record stays traceable.
        label = f"{source} {native}"

    latitude = coerce_float(lat)
    longitude = coerce_float(lon)
    if not valid_coords(latitude, longitude):
        latitude = longitude = None

    return Camera(
        source=source,
        native_id=native,
        name=label,
        country=(country or "").upper()[:2] or None,
        region=clean_text(region, limit=40),
        city=clean_text(city, limit=80),
        lat=latitude,
        lon=longitude,
        kind=kind if kind in KINDS else "other",
        image=image_url,
        stream=stream_url,
        stream_format=guess_stream_format(stream_url),
        attribution=attribution,
        license=license,
        source_url=source_url,
        tags=sorted({t for t in (tags or []) if t}),
    )
