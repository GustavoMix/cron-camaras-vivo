"""Pipeline: fetch every source, merge, de-duplicate and write the dataset.

Output layout (all under ``data/v1``)::

    index.json            metadata, per-country counts, source health
    cameras.json          every camera, sorted
    sources.json          catalog of sources with licence and attribution
    countries/<CC>.json   one shard per country, for clients that need a slice

Determinism is a hard requirement: the same input must produce byte-identical
files, otherwise every weekly commit is a whole-file diff.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .http import Http
from .models import SCHEMA_VERSION, Camera
from .sources import Source, SourceResult, select_sources

log = logging.getLogger(__name__)

DATA_VERSION = "v1"
UNKNOWN_COUNTRY = "ZZ"


@dataclass(slots=True)
class BuildReport:
    """Summary of one run, used for logging and the job summary."""

    generated_at: str
    total: int = 0
    duplicates: int = 0
    results: list[SourceResult] = field(default_factory=list)
    by_country: dict[str, int] = field(default_factory=dict)

    @property
    def ok_sources(self) -> list[SourceResult]:
        return [r for r in self.results if r.ok]

    @property
    def failed_sources(self) -> list[SourceResult]:
        return [r for r in self.results if not r.ok]


async def fetch_all(sources: list[Source], *, concurrency: int = 8) -> list[SourceResult]:
    """Run every source concurrently, isolating failures."""
    async with Http(concurrency=concurrency) as http:
        # Source.run never raises, so gather returns one result per source.
        return list(await asyncio.gather(*(source.run(http) for source in sources)))


def deduplicate(results: Iterable[SourceResult]) -> tuple[list[Camera], int]:
    """Merge cameras across sources, dropping repeats.

    Sources are processed in id order so that when two feeds publish the same
    camera, the winner is stable between runs rather than depending on which
    HTTP response landed first.
    """
    seen_keys: set[str] = set()
    seen_ids: set[str] = set()
    merged: list[Camera] = []
    duplicates = 0

    for result in sorted(results, key=lambda r: r.source_id):
        for camera in result.cameras:
            if camera.id in seen_ids:
                duplicates += 1
                continue

            keys = camera.dedupe_keys()
            if any(key in seen_keys for key in keys):
                duplicates += 1
                continue

            seen_ids.add(camera.id)
            seen_keys.update(keys)
            merged.append(camera)

    merged.sort(key=_sort_key)
    return merged, duplicates


def _sort_key(camera: Camera) -> tuple[str, str, str, str]:
    """Deterministic ordering: country, region, source, then id."""
    return (
        camera.country or UNKNOWN_COUNTRY,
        camera.region or "",
        camera.source,
        camera.id,
    )


def write_dataset(
    cameras: list[Camera],
    results: list[SourceResult],
    sources: list[Source],
    *,
    out_dir: Path,
    generated_at: str,
) -> BuildReport:
    """Write every output file. Returns the report for the caller to log."""
    root = out_dir / DATA_VERSION
    countries_dir = root / "countries"
    countries_dir.mkdir(parents=True, exist_ok=True)

    by_country: dict[str, list[Camera]] = defaultdict(list)
    for camera in cameras:
        by_country[camera.country or UNKNOWN_COUNTRY].append(camera)

    kind_counts = Counter(camera.kind for camera in cameras)
    with_coords = sum(1 for camera in cameras if camera.lat is not None)
    with_stream = sum(1 for camera in cameras if camera.stream)

    _write_json(
        root / "cameras.json",
        {
            "schema": SCHEMA_VERSION,
            "generated_at": generated_at,
            "count": len(cameras),
            "cameras": [camera.to_json() for camera in cameras],
        },
    )

    # Stale shards must go, or a country that disappears upstream keeps serving
    # last week's data forever.
    for stale in countries_dir.glob("*.json"):
        if stale.stem not in by_country:
            stale.unlink()

    for country, group in sorted(by_country.items()):
        _write_json(
            countries_dir / f"{country}.json",
            {
                "schema": SCHEMA_VERSION,
                "generated_at": generated_at,
                "country": country,
                "count": len(group),
                "cameras": [camera.to_json() for camera in group],
            },
        )

    _write_json(
        root / "sources.json",
        {
            "schema": SCHEMA_VERSION,
            "generated_at": generated_at,
            "sources": [
                {**source.describe(), **_result_for(results, source.id)}
                for source in sorted(sources, key=lambda s: s.id)
            ],
        },
    )

    _write_json(
        root / "index.json",
        {
            "schema": SCHEMA_VERSION,
            "generated_at": generated_at,
            "count": len(cameras),
            "stats": {
                "with_coordinates": with_coords,
                "with_stream": with_stream,
                "by_kind": dict(sorted(kind_counts.items())),
            },
            "countries": {
                country: len(group) for country, group in sorted(by_country.items())
            },
            "files": {
                "all": f"{DATA_VERSION}/cameras.json",
                "sources": f"{DATA_VERSION}/sources.json",
                "country": f"{DATA_VERSION}/countries/{{country}}.json",
            },
            "sources": [result.to_json() for result in sorted(results, key=lambda r: r.source_id)],
        },
    )

    return BuildReport(
        generated_at=generated_at,
        total=len(cameras),
        results=results,
        by_country={country: len(group) for country, group in sorted(by_country.items())},
    )


def _result_for(results: list[SourceResult], source_id: str) -> dict[str, Any]:
    for result in results:
        if result.source_id == source_id:
            return {"ok": result.ok, "cameras": result.count}
    return {"ok": None, "cameras": 0}


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write compact, deterministic JSON with a trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


async def build(
    *,
    out_dir: Path,
    only: list[str] | None = None,
    concurrency: int = 8,
    include_gated: bool = False,
    min_cameras: int = 0,
) -> BuildReport:
    """Run the whole pipeline and write the dataset."""
    sources = select_sources(only, skip_gated=not include_gated)
    if not sources:
        raise RuntimeError("no sources selected")

    log.info("running %d source(s)", len(sources))
    results = await fetch_all(sources, concurrency=concurrency)

    for result in results:
        if result.ok:
            log.info(
                "  %-12s %5d cameras in %.1fs",
                result.source_id,
                result.count,
                result.duration_s,
            )
        else:
            log.warning("  %-12s FAILED: %s", result.source_id, result.error)

    cameras, duplicates = deduplicate(results)

    # A near-empty dataset almost always means a network or upstream outage.
    # Refusing to write keeps a good dataset from being replaced by a bad one.
    if min_cameras and len(cameras) < min_cameras:
        failed = ", ".join(r.source_id for r in results if not r.ok) or "none"
        raise RuntimeError(
            f"only {len(cameras)} cameras collected, below the --min-cameras "
            f"threshold of {min_cameras}; refusing to overwrite the dataset "
            f"(failed sources: {failed})"
        )

    generated_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    report = write_dataset(
        cameras, results, sources, out_dir=out_dir, generated_at=generated_at
    )
    report.duplicates = duplicates
    return report
