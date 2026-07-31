"""Transport for London "JamCams".

TfL's unified API exposes cameras as Places. A key is optional for light use;
when ``TFL_APP_KEY`` is present it is sent to lift the rate limit.

Each Place carries its media in ``additionalProperties``::

    {"id": "JamCams_00001.00859", "commonName": "A2 Sun in the Sands",
     "lat": 51.47, "lon": 0.02,
     "additionalProperties": [
        {"key": "imageUrl", "value": "https://.../00001.00859.jpg"},
        {"key": "videoUrl", "value": "https://.../00001.00859.mp4"}]}
"""

from __future__ import annotations

import os
from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import first_of
from .base import Source

API_URL = "https://api.tfl.gov.uk/Place/Type/JamCam"


class TflJamCams(Source):
    id = "tfl-jamcams"
    name = "Transport for London"
    country = "GB"
    license = "Powered by TfL Open Data"
    homepage = "https://api.tfl.gov.uk/"

    async def fetch(self, http: Http) -> list[Camera]:
        params: dict[str, Any] = {}
        app_key = os.environ.get("TFL_APP_KEY", "").strip()
        if app_key:
            params["app_key"] = app_key

        payload = await http.get_json(API_URL, params=params or None)
        records = payload if isinstance(payload, list) else []

        cameras: list[Camera] = []
        for record in records:
            camera = self._parse(record)
            if camera:
                cameras.append(camera)
        return cameras

    def _parse(self, record: Any) -> Camera | None:
        if not isinstance(record, dict):
            return None

        native_id = first_of(record, "id", "naptanId")
        if native_id is None:
            return None

        props = _additional_properties(record)
        if str(props.get("available", "true")).strip().lower() == "false":
            return None

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=first_of(record, "commonName", "name"),
            country="GB",
            region="England",
            city="London",
            lat=first_of(record, "lat", "latitude"),
            lon=first_of(record, "lon", "longitude"),
            kind="traffic",
            image=props.get("imageurl"),
            stream=props.get("videourl"),
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=["traffic", "urban"],
        )


def _additional_properties(record: dict[str, Any]) -> dict[str, str]:
    """Flatten TfL's key/value property list into a lowercase-keyed dict."""
    result: dict[str, str] = {}
    entries = record.get("additionalProperties")
    if not isinstance(entries, list):
        return result
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        value = entry.get("value")
        if isinstance(key, str) and value not in (None, ""):
            result[key.strip().lower()] = str(value)
    return result


def build_sources() -> list[Source]:
    return [TflJamCams()]
