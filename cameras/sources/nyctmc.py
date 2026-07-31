"""New York City DOT traffic cameras.

The NYC Traffic Management Center publishes its camera list without a key.
Records look roughly like::

    {"id": "abc-123", "name": "1 Ave @ 42 St", "latitude": 40.7,
     "longitude": -73.9, "imageUrl": "https://.../image.jpg",
     "videoUrl": "https://.../index.m3u8", "isOnline": "true", "area": "Manhattan"}

The endpoint has been served both as a bare array and wrapped in an envelope,
so both are handled.
"""

from __future__ import annotations

from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import first_of
from .base import Source

API_URL = "https://webcams.nyctmc.org/api/cameras"


class NycTmc(Source):
    id = "nyc-dot"
    name = "New York City DOT"
    country = "US"
    license = "NYC Open Data terms of use"
    homepage = "https://webcams.nyctmc.org/"

    async def fetch(self, http: Http) -> list[Camera]:
        payload = await http.get_json(API_URL)
        records = _as_records(payload)

        cameras: list[Camera] = []
        for record in records:
            camera = self._parse(record)
            if camera:
                cameras.append(camera)
        return cameras

    def _parse(self, record: Any) -> Camera | None:
        if not isinstance(record, dict):
            return None
        # GeoJSON variant: the useful fields sit under "properties".
        if "properties" in record and isinstance(record["properties"], dict):
            record = {**record["properties"], **_geojson_point(record)}

        online = first_of(record, "isOnline", "online", "status")
        if str(online).strip().lower() in {"false", "offline", "0"}:
            return None

        native_id = first_of(record, "id", "cameraId", "uuid")
        if native_id is None:
            return None

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=first_of(record, "name", "commonName", "title"),
            country="US",
            region="NY",
            city=first_of(record, "area", "borough") or "New York",
            lat=first_of(record, "latitude", "lat"),
            lon=first_of(record, "longitude", "lon", "lng"),
            kind="traffic",
            image=first_of(record, "imageUrl", "image", "imageURL"),
            stream=first_of(record, "videoUrl", "video", "videoURL"),
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=["traffic", "urban"],
        )


def _geojson_point(feature: dict[str, Any]) -> dict[str, Any]:
    """Extract lat/lon from a GeoJSON geometry, if present."""
    geometry = feature.get("geometry")
    if not isinstance(geometry, dict):
        return {}
    coords = geometry.get("coordinates")
    if isinstance(coords, list) and len(coords) >= 2:
        # GeoJSON is [longitude, latitude].
        return {"longitude": coords[0], "latitude": coords[1]}
    return {}


def _as_records(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("cameras", "data", "features", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def build_sources() -> list[Source]:
    return [NycTmc()]
