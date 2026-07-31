"""Agencies running the Castle Rock "511" traveler-information platform.

Dozens of North American transport agencies deploy the same software, all
exposing ``/api/v2/get/cameras``. One adapter therefore covers many agencies:
each :class:`Castle511` instance is just a base URL plus metadata.

Response shape (per record)::

    {"Id": 123, "Organization": "...", "RoadwayName": "Highway 401",
     "DirectionOfTravel": "East", "Latitude": 43.6, "Longitude": -79.3,
     "Location": "Highway 401 near Yonge St",
     "Views": [{"Id": 1, "Url": "https://.../image.jpg",
                "Status": "Enabled", "Description": "..."}]}

Field casing and the presence of ``Views`` vary between deployments, so every
lookup goes through :func:`first_of` and missing pieces degrade to ``None``.
"""

from __future__ import annotations

import os
from typing import Any

from ..http import Http
from ..models import Camera, make_camera
from ..util import clean_text, first_of, guess_stream_format
from .base import Source


class Castle511(Source):
    """A single agency deployment of the 511 platform."""

    def __init__(
        self,
        *,
        source_id: str,
        name: str,
        base_url: str,
        country: str,
        region: str | None = None,
        key_env: str | None = None,
        license: str | None = None,
    ) -> None:
        self.id = source_id
        self.name = name
        self.country = country
        self.region = region
        self.base_url = base_url.rstrip("/")
        self.homepage = base_url
        self.license = license or f"Open data published by {name}"
        # Deployments that demand an API key are skipped unless one is supplied,
        # rather than burning a request on a guaranteed 401.
        self.requires_env = key_env

    async def fetch(self, http: Http) -> list[Camera]:
        params: dict[str, Any] = {"format": "json", "lang": "en"}
        if self.requires_env:
            key = os.environ.get(self.requires_env, "").strip()
            if not key:
                return []
            params["key"] = key

        payload = await http.get_json(f"{self.base_url}/api/v2/get/cameras", params=params)
        records = _as_records(payload)

        cameras: list[Camera] = []
        for record in records:
            cameras.extend(self._parse(record))
        return cameras

    def _parse(self, record: Any) -> list[Camera]:
        if not isinstance(record, dict):
            return []

        native_id = first_of(record, "Id", "ID", "id", "CameraId")
        if native_id is None:
            return []

        lat = first_of(record, "Latitude", "lat", "Lat")
        lon = first_of(record, "Longitude", "lon", "Lng", "Long")
        roadway = clean_text(first_of(record, "RoadwayName", "Roadway", "Route"))
        location = clean_text(first_of(record, "Location", "Description", "Name"))
        direction = clean_text(first_of(record, "DirectionOfTravel", "Direction"))

        # Build the most informative label the record supports.
        label = location or roadway
        if roadway and location and roadway.lower() not in location.lower():
            label = f"{roadway} - {location}"
        if direction and label and direction.lower() not in label.lower():
            label = f"{label} ({direction})"

        views = first_of(record, "Views", "views") or []
        if not isinstance(views, list):
            views = []

        cameras: list[Camera] = []
        for index, view in enumerate(views):
            if not isinstance(view, dict):
                continue
            status = str(first_of(view, "Status", "status") or "").strip().lower()
            if status in {"disabled", "offline", "unavailable"}:
                continue

            url = first_of(view, "Url", "URL", "url", "ImageUrl")
            if not url:
                continue

            # A deployment may expose several views (angles) per camera; each
            # needs its own id or they would collapse during de-duplication.
            view_id = first_of(view, "Id", "id") or index
            view_label = clean_text(first_of(view, "Description", "description"))
            name = label
            if view_label and (not label or view_label.lower() not in label.lower()):
                name = f"{label} - {view_label}" if label else view_label

            is_stream = guess_stream_format(str(url)) in {"hls", "dash", "mp4", "mjpeg"}
            camera = make_camera(
                source=self.id,
                native_id=f"{native_id}:{view_id}" if len(views) > 1 else str(native_id),
                name=name,
                country=self.country,
                region=self.region,
                lat=lat,
                lon=lon,
                kind="traffic",
                image=None if is_stream else url,
                stream=url if is_stream else None,
                attribution=self.name,
                license=self.license,
                source_url=self.homepage,
                tags=["traffic"],
            )
            if camera:
                cameras.append(camera)
        return cameras


def _as_records(payload: Any) -> list[Any]:
    """Unwrap the several envelopes these deployments use."""
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("Cameras", "cameras", "data", "Data", "results", "features"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


#: Deployments without an API key. These are the ones expected to work out of
#: the box; every entry is best-effort and reported in the health file.
_OPEN: list[tuple[str, str, str, str, str | None]] = [
    ("511on", "Ontario 511", "https://511on.ca", "CA", "ON"),
    ("511ab", "Alberta 511", "https://511.alberta.ca", "CA", "AB"),
    ("511nb", "New Brunswick 511", "https://511.gnb.ca", "CA", "NB"),
    ("511ns", "Nova Scotia 511", "https://511.novascotia.ca", "CA", "NS"),
    ("511pei", "Prince Edward Island 511", "https://511.gov.pe.ca", "CA", "PE"),
    ("511sk", "Saskatchewan Highway Hotline", "https://saskatchewan.ca", "CA", "SK"),
    ("511mb", "Manitoba 511", "https://www.manitoba511.ca", "CA", "MB"),
]

#: Deployments that gate the API behind a free key. Set the matching secret to
#: enable them; without it the source is skipped cleanly.
_KEYED: list[tuple[str, str, str, str, str | None, str]] = [
    ("511ny", "511NY", "https://511ny.org", "US", "NY", "KEY_511NY"),
    ("fl511", "FL511", "https://fl511.com", "US", "FL", "KEY_FL511"),
    ("511ga", "511 Georgia", "https://511ga.org", "US", "GA", "KEY_511GA"),
    ("mass511", "Mass511", "https://mass511.com", "US", "MA", "KEY_MASS511"),
    ("511pa", "511PA", "https://www.511pa.com", "US", "PA", "KEY_511PA"),
    ("511va", "511 Virginia", "https://www.511virginia.org", "US", "VA", "KEY_511VA"),
    ("udot", "UDOT Traffic", "https://www.udottraffic.utah.gov", "US", "UT", "KEY_UDOT"),
    ("511ia", "511 Iowa", "https://511ia.org", "US", "IA", "KEY_511IA"),
    ("511wi", "511 Wisconsin", "https://511wi.gov", "US", "WI", "KEY_511WI"),
    ("511ne", "511 Nebraska", "https://511.nebraska.gov", "US", "NE", "KEY_511NE"),
]


def build_sources() -> list[Source]:
    """Instantiate every known 511 deployment."""
    sources: list[Source] = [
        Castle511(
            source_id=source_id,
            name=name,
            base_url=base_url,
            country=country,
            region=region,
        )
        for source_id, name, base_url, country, region in _OPEN
    ]
    sources.extend(
        Castle511(
            source_id=source_id,
            name=name,
            base_url=base_url,
            country=country,
            region=region,
            key_env=key_env,
        )
        for source_id, name, base_url, country, region, key_env in _KEYED
    )
    return sources
