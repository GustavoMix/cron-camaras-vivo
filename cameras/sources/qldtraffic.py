"""Queensland Traffic webcams (Australia), served as GeoJSON.

The QLDTraffic open API needs a free key (``QLD_TRAFFIC_KEY``). Features look
like::

    {"type": "Feature",
     "geometry": {"type": "Point", "coordinates": [153.02, -27.47]},
     "properties": {"id": "1234", "description": "Bruce Hwy at Burpengary",
                    "image_url": "https://.../cam.jpg", "locality": "Burpengary"}}
"""

from __future__ import annotations

import os
from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import first_of
from .base import Source

API_URL = "https://api.qldtraffic.qld.gov.au/v2/webcams"


class QldTraffic(Source):
    id = "qld-traffic"
    name = "Queensland Department of Transport and Main Roads"
    country = "AU"
    license = "Creative Commons Attribution 4.0 (QLDTraffic open data)"
    homepage = "https://qldtraffic.qld.gov.au/"
    requires_env = "QLD_TRAFFIC_KEY"

    async def fetch(self, http: Http) -> list[Camera]:
        api_key = os.environ.get("QLD_TRAFFIC_KEY", "").strip()
        if not api_key:
            return []

        payload = await http.get_json(API_URL, params={"apikey": api_key})
        features = payload.get("features") if isinstance(payload, dict) else payload
        if not isinstance(features, list):
            return []

        cameras: list[Camera] = []
        for feature in features:
            camera = self._parse(feature)
            if camera:
                cameras.append(camera)
        return cameras

    def _parse(self, feature: Any) -> Camera | None:
        if not isinstance(feature, dict):
            return None

        properties = feature.get("properties")
        props = properties if isinstance(properties, dict) else feature
        geometry = feature.get("geometry")
        geometry = geometry if isinstance(geometry, dict) else {}
        coords = geometry.get("coordinates")
        coords = coords if isinstance(coords, list) else []

        lat = coords[1] if len(coords) >= 2 else first_of(props, "latitude", "lat")
        lon = coords[0] if len(coords) >= 2 else first_of(props, "longitude", "lon")

        native_id = first_of(props, "id", "webcam_id")
        if native_id is None:
            return None

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=first_of(props, "description", "name", "title"),
            country="AU",
            region="QLD",
            city=first_of(props, "locality", "suburb"),
            lat=lat,
            lon=lon,
            kind="traffic",
            image=first_of(props, "image_url", "imageUrl", "url"),
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=["traffic"],
        )


def build_sources() -> list[Source]:
    return [QldTraffic()]
