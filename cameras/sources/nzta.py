"""Waka Kotahi NZ Transport Agency highway cameras (New Zealand).

Open REST endpoint, no key required. Despite the service's own XML default,
requesting it with an ``Accept: application/json`` header (which every
adapter sends via :class:`~cameras.http.Http`) returns JSON instead, which is
what this adapter parses. Response shape (per record)::

    {"id": 714, "name": "SH1 Tinwald", "offline": false,
     "description": "South along Hinds Highway from Lagmhor Rd",
     "latitude": -43.919632, "longitude": 171.721055,
     "imageUrl": "/camera/714.jpg", "region": {"id": 11, "name": "Canterbury"}}

``imageUrl`` is relative to the service host, so it is resolved against
:data:`BASE_URL` before being handed to :func:`make_camera`.
"""

from __future__ import annotations

from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import first_of
from .base import Source

BASE_URL = "https://trafficnz.info"
API_URL = f"{BASE_URL}/service/traffic/rest/4/cameras/all"


class NztaCameras(Source):
    id = "nzta"
    name = "Waka Kotahi NZ Transport Agency"
    country = "NZ"
    license = "Waka Kotahi open data terms of use"
    homepage = "https://www.nzta.govt.nz/traffic-and-travel-information/use-our-data/"

    async def fetch(self, http: Http) -> list[Camera]:
        payload = await http.get_json(API_URL)
        response = payload.get("response") if isinstance(payload, dict) else None
        records = response.get("camera") if isinstance(response, dict) else None
        if not isinstance(records, list):
            return []

        cameras: list[Camera] = []
        for record in records:
            camera = self._parse(record)
            if camera:
                cameras.append(camera)
        return cameras

    def _parse(self, record: Any) -> Camera | None:
        if not isinstance(record, dict):
            return None
        if str(record.get("offline")).strip().lower() == "true":
            return None

        native_id = first_of(record, "id")
        if native_id is None:
            return None

        image_path = first_of(record, "imageUrl")
        image = f"{BASE_URL}{image_path}" if image_path else None

        region = record.get("region")
        region_name = first_of(region, "name") if isinstance(region, dict) else None

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=first_of(record, "name", "description"),
            country="NZ",
            region=region_name,
            lat=first_of(record, "latitude"),
            lon=first_of(record, "longitude"),
            kind="traffic",
            image=image,
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=["traffic"],
        )


def build_sources() -> list[Source]:
    return [NztaCameras()]
