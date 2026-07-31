"""Windy Webcams API v3 - the one genuinely global source in the registry.

Windy aggregates tens of thousands of public webcams worldwide (scenic,
coastal, mountain, urban). It requires a free API key, so the source stays
disabled until ``WINDY_API_KEY`` is set; everything else keeps working without
it.

The API pages at 50 records per request, so this walks the offset until the
reported total is reached or the page comes back empty.
"""

from __future__ import annotations

import os
from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import first_of
from .base import Source

API_URL = "https://api.windy.com/webcams/api/v3/webcams"
PAGE_SIZE = 50
# Guard against a pagination bug turning into an unbounded request loop.
MAX_PAGES = 400


class Windy(Source):
    id = "windy"
    name = "Windy.com Webcams"
    country = None
    license = "Windy Webcams API terms - attribution required"
    homepage = "https://www.windy.com/webcams"
    requires_env = "WINDY_API_KEY"

    async def fetch(self, http: Http) -> list[Camera]:
        api_key = os.environ.get("WINDY_API_KEY", "").strip()
        if not api_key:
            return []

        headers = {"x-windy-api-key": api_key}
        cameras: list[Camera] = []
        seen_ids: set[str] = set()
        offset = 0

        for _ in range(MAX_PAGES):
            payload = await http.get_json(
                API_URL,
                params={
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "include": "location,images,urls,categories,player",
                },
                headers=headers,
            )
            if not isinstance(payload, dict):
                break

            page = payload.get("webcams")
            if not isinstance(page, list) or not page:
                break

            for record in page:
                camera = self._parse(record)
                if camera and camera.native_id not in seen_ids:
                    seen_ids.add(camera.native_id)
                    cameras.append(camera)

            offset += len(page)
            total = payload.get("total")
            if isinstance(total, int) and offset >= total:
                break
            if len(page) < PAGE_SIZE:
                break

        return cameras

    def _parse(self, record: Any) -> Camera | None:
        if not isinstance(record, dict):
            return None

        native_id = first_of(record, "webcamId", "id")
        if native_id is None:
            return None

        status = str(first_of(record, "status") or "active").strip().lower()
        if status in {"inactive", "disabled"}:
            return None

        location = record.get("location") if isinstance(record.get("location"), dict) else {}
        images = record.get("images") if isinstance(record.get("images"), dict) else {}
        current = images.get("current") if isinstance(images.get("current"), dict) else {}
        player = record.get("player") if isinstance(record.get("player"), dict) else {}

        return make_camera(
            source=self.id,
            native_id=native_id,
            name=first_of(record, "title", "name"),
            country=first_of(location, "country_code", "countryCode", "country"),
            region=first_of(location, "region", "state"),
            city=first_of(location, "city"),
            lat=first_of(location, "latitude", "lat"),
            lon=first_of(location, "longitude", "lon"),
            kind=_kind_from_categories(record),
            image=first_of(current, "preview", "thumbnail", "icon"),
            stream=first_of(player, "live", "day", "month"),
            attribution=self.name,
            license=self.license,
            source_url=self.homepage,
            tags=_tags_from_categories(record),
        )


def _categories(record: dict[str, Any]) -> list[str]:
    raw = record.get("categories")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for entry in raw:
        value = entry.get("id") or entry.get("name") if isinstance(entry, dict) else entry
        if isinstance(value, str) and value.strip():
            names.append(value.strip().lower())
    return names


def _kind_from_categories(record: dict[str, Any]) -> str:
    names = set(_categories(record))
    if names & {"traffic", "highway"}:
        return "traffic"
    if names & {"airport", "airfield"}:
        return "airport"
    if names & {"harbor", "harbour", "port"}:
        return "port"
    if names & {"weather", "meteo"}:
        return "weather"
    if names & {"beach", "mountain", "landscape", "city", "island", "lake", "park"}:
        return "scenic"
    return "scenic"


def _tags_from_categories(record: dict[str, Any]) -> list[str]:
    return sorted(set(_categories(record)))[:8]


def build_sources() -> list[Source]:
    return [Windy()]
