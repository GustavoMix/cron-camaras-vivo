"""Hand-curated cameras with per-camera rights evidence.

This is the primary path for the first 30-50 cameras: a person confirms that a
publisher allows commercial redistribution, records the evidence, and the
pipeline takes over verification and health tracking from there.

``curated/cameras.json`` format::

    {"cameras": [{
        "native_id": "nps-old-faithful",
        "name": "Old Faithful", "country": "US", "city": "Yellowstone",
        "lat": 44.46, "lon": -110.83, "kind": "scenic", "tags": ["geyser"],
        "stream": "https://.../index.m3u8", "image": "https://.../thumb.jpg",
        "attribution": "National Park Service",
        "rights": {"license": "Public domain (US Gov)", "commercial_use": true,
                   "evidence_url": "https://...", "verified_on": "2026-11-11",
                   "review_by": "2027-11-11"}
    }]}
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from .base import Source

DEFAULT_PATH = Path("curated/cameras.json")


class Manual(Source):
    id = "manual"
    name = "Hand-curated cameras"
    license = "Per-camera; see each entry's rights block"
    homepage = "https://github.com/GustavoMix/cron-camaras-vivo"

    def __init__(self, path: Path = DEFAULT_PATH) -> None:
        self.path = path

    async def fetch(self, http: Http) -> list[Camera]:
        if not self.path.exists():
            return []
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        records = payload.get("cameras") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise ValueError(f"{self.path}: expected an object with a 'cameras' list")
        cameras = (self._parse(record) for record in records)
        return [camera for camera in cameras if camera]

    def _parse(self, record: Any) -> Camera | None:
        if not isinstance(record, dict):
            return None
        rights = record.get("rights") if isinstance(record.get("rights"), dict) else None
        license_text = rights.get("license") if rights else None
        return make_camera(
            source=self.id,
            native_id=record.get("native_id"),
            name=record.get("name"),
            country=record.get("country"),
            region=record.get("region"),
            city=record.get("city"),
            lat=record.get("lat"),
            lon=record.get("lon"),
            kind=record.get("kind", "scenic"),
            image=record.get("image"),
            stream=record.get("stream"),
            attribution=record.get("attribution"),
            license=license_text,
            source_url=record.get("source_url"),
            tags=record.get("tags"),
            rights=rights,
        )


def build_sources() -> list[Source]:
    return [Manual()]
