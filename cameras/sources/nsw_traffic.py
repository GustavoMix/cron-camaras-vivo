"""Transport for NSW live traffic cameras (Australia), served via ArcGIS.

Open data hosted on the NSW government's ArcGIS portal, no key required.
Requesting ``outSR=4326`` gets the feature service to hand back plain
lat/lon instead of Web Mercator, so no projection math is needed here.

Response shape (per feature)::

    {"attributes": {"id": "023651ee-...", "region": "SYD_SOUTH",
                     "title": "5 Ways (Miranda)",
                     "view": "5 Ways at The Boulevarde looking west...",
                     "href": "https://webcams.transport.nsw.gov.au/.../x.jpeg"},
     "geometry": {"x": 151.105, "y": -34.029}}
"""

from __future__ import annotations

from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import first_of
from .base import Source

QUERY_URL = (
    "https://portal.data.nsw.gov.au/arcgis/rest/services/Hosted/"
    "TfNSW_Traffic_Cameras_Public/FeatureServer/0/query"
)


class NswTraffic(Source):
    id = "nsw-traffic"
    name = "Transport for NSW"
    country = "AU"
    license = "Transport for NSW Open Data Hub terms of use"
    homepage = "https://opendata.transport.nsw.gov.au/dataset/live-traffic-cameras"

    async def fetch(self, http: Http) -> list[Camera]:
        payload = await http.get_json(
            QUERY_URL,
            params={
                "where": "1=1",
                "outFields": "*",
                "outSR": "4326",
                "f": "json",
                "resultRecordCount": "1000",
            },
        )
        features = payload.get("features") if isinstance(payload, dict) else None
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

        props = feature.get("attributes")
        props = props if isinstance(props, dict) else {}
        geometry = feature.get("geometry")
        geometry = geometry if isinstance(geometry, dict) else {}

        native_id = first_of(props, "id", "objectid")
        if native_id is None:
            return None

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=first_of(props, "title", "view"),
            country="AU",
            region=first_of(props, "region"),
            lat=geometry.get("y"),
            lon=geometry.get("x"),
            kind="traffic",
            image=first_of(props, "href"),
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=["traffic"],
        )


def build_sources() -> list[Source]:
    return [NswTraffic()]
