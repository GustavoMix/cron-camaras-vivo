"""Washington State DOT traveler information cameras.

WSDOT requires a free access code, so the source is skipped unless
``WSDOT_ACCESS_CODE`` is set. Records look like::

    {"CameraID": 1234, "Title": "I-5 at NE 45th St",
     "CameraLocation": {"Latitude": 47.6, "Longitude": -122.3,
                        "Description": "...", "RoadName": "I-5"},
     "ImageURL": "https://images.wsdot.wa.gov/nw/005vc16040.jpg",
     "IsActive": true}
"""

from __future__ import annotations

import os
from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import clean_text, first_of
from .base import Source

API_URL = (
    "https://www.wsdot.wa.gov/Traffic/api/HighwayCameras/HighwayCamerasREST.svc/"
    "GetCamerasAsJson"
)


class Wsdot(Source):
    id = "wsdot"
    name = "Washington State DOT"
    country = "US"
    license = "WSDOT traveler information API terms"
    homepage = "https://wsdot.wa.gov/traffic/api/"
    requires_env = "WSDOT_ACCESS_CODE"

    async def fetch(self, http: Http) -> list[Camera]:
        access_code = os.environ.get("WSDOT_ACCESS_CODE", "").strip()
        if not access_code:
            return []

        payload = await http.get_json(API_URL, params={"AccessCode": access_code})
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
        if first_of(record, "IsActive") is False:
            return None

        native_id = first_of(record, "CameraID", "CameraId", "id")
        if native_id is None:
            return None

        location = (
            record.get("CameraLocation")
            if isinstance(record.get("CameraLocation"), dict)
            else {}
        )
        road = clean_text(first_of(location, "RoadName"))
        title = clean_text(first_of(record, "Title", "Description"))
        name = title
        if road and title and road.lower() not in title.lower():
            name = f"{road} - {title}"

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=name,
            country="US",
            region="WA",
            city=first_of(location, "Description"),
            lat=first_of(location, "Latitude"),
            lon=first_of(location, "Longitude"),
            kind="traffic",
            image=first_of(record, "ImageURL", "ImageUrl"),
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=["traffic"],
        )


def build_sources() -> list[Source]:
    return [Wsdot()]
