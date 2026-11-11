"""Build the v2 catalog consumed by the Tizen app.

Files (under ``data/v2``)::

    manifest.json   tiny; the app polls this (ETag/304) to learn if anything changed
    catalog.json    published cameras only: verified rights, verified playback
    health.json     run diagnostics (not consumed by the app)
    review.json     cameras from sources still awaiting a rights decision

``version`` is a content hash, and ``updated_at`` only moves when the content
does, so a quiet run produces no diff in the published files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .classify import CATEGORIES
from .rights import Verdict
from .state import uptime

SCHEMA = 2
DIR = "v2"


def catalog_camera(entry: dict[str, Any], verdict: Verdict, quality: int) -> dict[str, Any]:
    meta = entry["meta"]
    probe = entry.get("probe") or {}

    location = {
        key: meta[key] for key in ("country", "region", "city", "lat", "lon") if key in meta
    }
    if probe.get("media") in {"hls", "mp4", "dash"} and meta.get("stream"):
        media: dict[str, Any] = {
            "type": "video",
            "url": meta["stream"],
            "format": probe["media"],
        }
        for key in ("width", "height", "codecs"):
            if key in probe:
                media[key] = probe[key]
        if meta.get("image"):
            media["thumbnail"] = meta["image"]
    else:
        media = {"type": "image", "url": meta.get("image"), "refresh_s": 60}
        media["thumbnail"] = meta.get("image")

    camera: dict[str, Any] = {
        "id": meta["id"],
        "name": meta["name"],
        "category": meta["category"],
        "tags": meta.get("tags", []),
        "location": location,
        "media": media,
        "quality": {
            "score": quality,
            "status": entry["status"],
            "uptime": round(uptime(entry), 2),
            "first_seen": entry["first_seen"],
            "last_verified": (entry.get("last_ok") or "")[:10] or None,
        },
        "rights": {
            "license": verdict.license or meta.get("license"),
            "attribution": verdict.attribution or meta.get("attribution"),
            "source": meta["source"],
            "source_url": meta.get("source_url"),
            "evidence_url": verdict.evidence_url,
            "verified_on": verdict.verified_on,
            "commercial_use": verdict.commercial,
        },
    }
    return camera


def _canonical(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def write_catalog(
    cameras: list[dict[str, Any]],
    out_dir: Path,
    generated_at: str,
    base_url: str = "",
    test: bool = False,
) -> tuple[bool, str]:
    """Write catalog + manifest. Returns ``(changed, version)``."""
    root = out_dir / DIR
    root.mkdir(parents=True, exist_ok=True)
    version = hashlib.sha256(_canonical(cameras).encode()).hexdigest()[:12]

    manifest_path = root / "manifest.json"
    previous: dict[str, Any] = {}
    if manifest_path.exists():
        previous = json.loads(manifest_path.read_text(encoding="utf-8"))

    if previous.get("version") == version and (root / "catalog.json").exists():
        return False, version

    counts = dict.fromkeys(CATEGORIES, 0)
    for camera in cameras:
        counts[camera["category"]] = counts.get(camera["category"], 0) + 1

    catalog = {
        "schema": SCHEMA,
        "version": version,
        "updated_at": generated_at,
        "count": len(cameras),
        **({"test": True} if test else {}),
        "categories": [{"id": c, "count": n} for c, n in counts.items() if n],
        "cameras": cameras,
    }
    _dump(root / "catalog.json", catalog)
    _dump(
        manifest_path,
        {
            "schema": SCHEMA,
            "version": version,
            "updated_at": generated_at,
            "count": len(cameras),
            **({"test": True} if test else {}),
            "catalog": f"{base_url}{DIR}/catalog.json",
        },
    )
    return True, version


def _dump(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n", encoding="utf-8"
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump(path, payload)
