"""Caltrans CCTV cameras, published per district as static JSON.

California publishes one file per district (1-12) with no key required::

    {"data": [{"cctv": {
        "index": "1",
        "location": {"district": "3", "locationName": "SR-99 at Elkhorn",
                     "nearbyPlace": "Sacramento", "county": "Sacramento",
                     "route": "99", "latitude": "38.6", "longitude": "-121.5"},
        "inService": "true",
        "imageData": {"streamingVideoURL": "https://.../index.m3u8",
                      "static": {"currentImageURL": "https://.../image.jpg"}}}}]}

Districts are fetched concurrently; a district that is down only costs its own
records.
"""

from __future__ import annotations

import asyncio
from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import clean_text, first_of
from .base import Source

DISTRICTS = (3, 4, 5, 6, 7, 8, 10, 11, 12)
URL_TEMPLATE = "https://cwwp2.dot.ca.gov/data/d{district}/cctv/cctvStatusD{padded}.json"


class Caltrans(Source):
    id = "caltrans"
    name = "California Department of Transportation"
    country = "US"
    license = "Public domain (Caltrans open data)"
    homepage = "https://cwwp2.dot.ca.gov/vm/iframemap.htm"

    async def fetch(self, http: Http) -> list[Camera]:
        tasks = [self._fetch_district(http, district) for district in DISTRICTS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        cameras: list[Camera] = []
        failures = 0
        for result in results:
            if isinstance(result, BaseException):
                failures += 1
                continue
            cameras.extend(result)

        # If every district failed the source itself is broken, so surface it.
        if failures == len(DISTRICTS):
            raise RuntimeError(f"all {failures} Caltrans districts failed")
        return cameras

    async def _fetch_district(self, http: Http, district: int) -> list[Camera]:
        url = URL_TEMPLATE.format(district=district, padded=f"{district:02d}")
        payload = await http.get_json(url)
        records = payload.get("data", []) if isinstance(payload, dict) else []
        if not isinstance(records, list):
            return []

        cameras: list[Camera] = []
        for record in records:
            camera = self._parse(record, district)
            if camera:
                cameras.append(camera)
        return cameras

    def _parse(self, record: Any, district: int) -> Camera | None:
        if not isinstance(record, dict):
            return None
        cctv = record.get("cctv") if isinstance(record.get("cctv"), dict) else record
        if not isinstance(cctv, dict):
            return None

        # "false" as a string is the common case here, so compare textually.
        if str(first_of(cctv, "inService") or "true").strip().lower() == "false":
            return None

        location = cctv.get("location") if isinstance(cctv.get("location"), dict) else {}
        image_data = cctv.get("imageData") if isinstance(cctv.get("imageData"), dict) else {}
        static = image_data.get("static") if isinstance(image_data.get("static"), dict) else {}

        index = first_of(cctv, "index", "recordId")
        route = clean_text(first_of(location, "route"))
        location_name = clean_text(first_of(location, "locationName"))
        nearby = clean_text(first_of(location, "nearbyPlace"))
        county = clean_text(first_of(location, "county"))

        name = location_name or nearby or f"District {district} camera {index}"
        if route and location_name and route not in location_name:
            name = f"SR-{route} {location_name}"

        native_id = f"d{district}-{index}" if index is not None else None
        if native_id is None:
            return None

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=name,
            country="US",
            region="CA",
            city=nearby or county,
            lat=first_of(location, "latitude", "lat"),
            lon=first_of(location, "longitude", "lon"),
            kind="traffic",
            image=first_of(static, "currentImageURL", "currentImageUrl"),
            stream=first_of(image_data, "streamingVideoURL", "streamingVideoUrl"),
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=["traffic", f"caltrans-d{district}"],
        )


def build_sources() -> list[Source]:
    return [Caltrans()]
