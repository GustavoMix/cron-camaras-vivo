"""Curation pipeline: discover -> filter -> rights -> probe (adaptive) -> publish.

Cost controls, in the order they bite:

1. Only sources the rights policy allows are fetched at all (others are
   fetched only with ``include_pending``, for the human review report).
2. Off-theme cameras (traffic, airports) are dropped before any probing.
3. Probes are adaptive: proven-stable cameras are checked every 24h instead of
   every run; offline ones every 48h; retired ones weekly.
4. A wall-clock budget caps probing; leftovers simply wait for the next run.
5. Two safety nets: an outage guard (mass failure => runner problem, not
   camera problem => record nothing) and a drop guard (refuse to publish a
   catalog that lost most of its cameras at once).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from ..build import deduplicate, fetch_all
from ..http import USER_AGENT
from ..models import Camera
from ..sources import select_sources
from . import state as st
from .catalog import catalog_camera, write_catalog, write_json
from .classify import classify
from .probe import PLAYABLE_FORMATS, Prober
from .rights import PUBLISHABLE, RightsPolicy
from .scoring import score

log = logging.getLogger(__name__)

PROBE_TIMEOUT = httpx.Timeout(connect=8.0, read=12.0, write=8.0, pool=8.0)
MISSING_GRACE = timedelta(days=14)
OUTAGE_MIN_SAMPLE = 10
OUTAGE_FAIL_RATIO = 0.7


@dataclass(slots=True)
class CurateConfig:
    out_dir: Path = Path("data")
    state_path: Path = Path("state/cameras-state.json")
    policy_path: Path = Path("policy/rights.json")
    only: list[str] | None = None
    include_pending: bool = False
    max_catalog: int = 300
    min_score: int = 55
    per_country_cap: int = 40
    per_source_cap: int = 120
    probe_budget_s: float = 480.0
    probe_concurrency: int = 12
    fetch_concurrency: int = 8
    force_all: bool = False
    max_drop_ratio: float = 0.5
    deep_wait_s: float = 4.0
    base_url: str = ""
    transport: httpx.AsyncBaseTransport | None = None  # tests inject a mock


@dataclass(slots=True)
class CurateReport:
    generated_at: str
    published: int = 0
    changed: bool = False
    version: str = ""
    refused: str | None = None
    outage_suspected: bool = False
    stats: dict[str, Any] = field(default_factory=dict)


def probe_target(meta: dict[str, Any]) -> tuple[str, str | None] | None:
    """Pick the media URL to verify: playable stream first, then still image."""
    if meta.get("stream") and meta.get("stream_format") in PLAYABLE_FORMATS:
        return meta["stream"], meta["stream_format"]
    if meta.get("image"):
        return meta["image"], None
    return None


def _meta(camera: Camera, category: str) -> dict[str, Any]:
    payload = camera.to_json()
    payload["category"] = category
    if camera.stream_format:
        payload["stream_format"] = camera.stream_format
    if camera.rights:
        payload["rights"] = camera.rights
    return payload


async def curate(config: CurateConfig, now: datetime | None = None) -> CurateReport:
    started = time.monotonic()
    now = (now or datetime.now(UTC)).replace(microsecond=0)
    today: date = now.date()
    report = CurateReport(generated_at=st.iso(now))
    policy = RightsPolicy.load(config.policy_path)
    store = st.StateStore.load(config.state_path)
    stats: Counter[str] = Counter()
    rejected: Counter[str] = Counter()

    # 1. Discovery, restricted to sources worth fetching.
    wanted = policy.source_ids(PUBLISHABLE) | {"manual"}
    if config.include_pending:
        wanted |= policy.source_ids("pending")
    sources = [s for s in select_sources(config.only) if s.id in wanted]
    results = await fetch_all(sources, concurrency=config.fetch_concurrency)
    fetched_ok = {r.source_id for r in results if r.ok}
    for result in results:
        if not result.ok:
            log.warning("source %s failed: %s", result.source_id, result.error)
    cameras, duplicates = deduplicate(results)
    stats["duplicates"] = duplicates

    # 2-3. Theme + rights gate; everything that passes is tracked in state.
    review: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for camera in cameras:
        category = classify(camera)
        if category is None:
            stats["off_theme"] += 1
            continue
        if probe_target(_meta(camera, category)) is None:
            stats["no_playable_media"] += 1
            continue
        verdict = policy.verdict(camera.source, camera.rights, today)
        if not verdict.ok:
            rejected[verdict.reason] += 1
            if config.include_pending and verdict.reason.startswith("status_pending"):
                review.append(
                    {"id": camera.id, "source": camera.source, "name": camera.name,
                     "country": camera.country, "category": category}
                )
            continue
        meta = _meta(camera, category)
        meta["id"] = camera.id
        seen_ids.add(camera.id)
        entry = store.entries.get(camera.id)
        if entry is None:
            store.entries[camera.id] = st.new_entry(meta, now)
            stats["new"] += 1
        else:
            entry["meta"] = meta
            entry["last_seen"] = today.isoformat()

    # Vanished upstream (source answered, camera absent) -> retire after grace.
    for cam_id, entry in store.entries.items():
        if cam_id in seen_ids or entry["meta"]["source"] not in fetched_ok:
            continue
        last_seen = date.fromisoformat(entry["last_seen"])
        if today - last_seen > MISSING_GRACE and entry["status"] != "retired":
            entry["status"] = "retired"
            stats["retired_missing"] += 1

    # 4. Adaptive probing under a time budget.
    queue = [
        (cam_id, entry)
        for cam_id, entry in store.entries.items()
        if entry["status"] != "retired" or entry["meta"]["source"] in fetched_ok
        if config.force_all or st.is_due(entry, now)
    ]
    # Newcomers first (they are what is blocking promotion), then stalest.
    queue.sort(key=lambda item: (item[1]["status"] != "candidate", item[1]["last_checked"] or ""))
    stats["tracked"] = len(store.entries)
    stats["due"] = len(queue)

    outcomes = await _probe_all(queue, config, started)
    stats["probed"] = len(outcomes)
    stats["skipped_budget"] = len(queue) - len(outcomes)

    failures = [o for o in outcomes if not o[2].ok]
    if len(outcomes) >= OUTAGE_MIN_SAMPLE and len(failures) / len(outcomes) >= OUTAGE_FAIL_RATIO:
        report.outage_suspected = True
        log.error(
            "%d/%d probes failed; assuming a network problem and keeping state as is",
            len(failures), len(outcomes),
        )
    else:
        for _cam_id, entry, result in outcomes:
            before = entry["status"]
            st.apply_probe(entry, result.to_json(), result.ok, now)
            if before != entry["status"]:
                stats[f"to_{entry['status']}"] += 1
            if not result.ok and result.reason:
                rejected[f"probe_{result.reason.split(':')[0]}"] += 1

    # 5. Selection: published statuses, rights still valid, score above floor.
    candidates: list[tuple[int, str, dict[str, Any]]] = []
    for cam_id, entry in store.entries.items():
        if entry["status"] not in st.PUBLISHED:
            continue
        meta = entry["meta"]
        verdict = policy.verdict(meta["source"], meta.get("rights"), today)
        if not verdict.ok:
            rejected[f"published_{verdict.reason}"] += 1
            continue
        if (entry.get("probe") or {}).get("tizen_ok") is False:
            rejected["not_tizen_playable"] += 1
            continue
        quality = score(entry, now)
        if quality < config.min_score:
            rejected["below_min_score"] += 1
            continue
        candidates.append((quality, cam_id, catalog_camera(entry, verdict, quality)))

    candidates.sort(key=lambda c: (-c[0], c[1]))
    per_country: Counter[str] = Counter()
    per_source: Counter[str] = Counter()
    chosen: list[dict[str, Any]] = []
    for _quality, _cam_id, camera in candidates:
        country = camera["location"].get("country", "ZZ")
        source = camera["rights"]["source"]
        if (
            len(chosen) >= config.max_catalog
            or per_country[country] >= config.per_country_cap
            or per_source[source] >= config.per_source_cap
        ):
            rejected["diversity_cap"] += 1
            continue
        per_country[country] += 1
        per_source[source] += 1
        chosen.append(camera)
    chosen.sort(key=lambda c: (-c["quality"]["score"], c["id"]))

    # 6. Publish, unless it looks like a catastrophe.
    previous_count = _previous_count(config.out_dir)
    if previous_count >= 10 and len(chosen) < previous_count * (1 - config.max_drop_ratio):
        report.refused = (
            f"catalog would drop from {previous_count} to {len(chosen)} cameras "
            f"(> {config.max_drop_ratio:.0%}); keeping the published version"
        )
        log.error(report.refused)
    else:
        report.changed, report.version = write_catalog(
            chosen, config.out_dir, report.generated_at, config.base_url
        )
    report.published = len(chosen)

    statuses = Counter(e["status"] for e in store.entries.values())
    report.stats = {
        **stats,
        "by_status": dict(sorted(statuses.items())),
        "rejected": dict(sorted(rejected.items())),
        "sources_ok": sorted(fetched_ok),
        "sources_failed": sorted(r.source_id for r in results if not r.ok),
        "duration_s": round(time.monotonic() - started, 1),
    }
    if not report.outage_suspected:
        store.save(config.state_path)
    health = {"schema": 1, "generated_at": report.generated_at, "published": report.published,
              "version": report.version, "refused": report.refused,
              "outage_suspected": report.outage_suspected, **report.stats}
    write_json(config.out_dir / "v2" / "health.json", health)
    if config.include_pending:
        write_json(
            config.out_dir / "v2" / "review.json",
            {"schema": 1, "generated_at": report.generated_at, "cameras": review[:500]},
        )
    return report


async def _probe_all(
    queue: list[tuple[str, dict[str, Any]]], config: CurateConfig, started: float
) -> list[tuple[str, dict[str, Any], Any]]:
    deadline = started + config.probe_budget_s
    async with httpx.AsyncClient(
        timeout=PROBE_TIMEOUT,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT},
        transport=config.transport,
    ) as client:
        prober = Prober(
            client, concurrency=config.probe_concurrency, deep_wait_s=config.deep_wait_s
        )

        async def one(cam_id: str, entry: dict[str, Any]):
            if time.monotonic() > deadline:
                return None
            target = probe_target(entry["meta"])
            if target is None:
                return None
            url, fmt = target
            deep = entry["status"] in {"candidate", "degraded", "offline"} and fmt is not None
            return cam_id, entry, await prober.probe(url, fmt, deep=deep)

        done = await asyncio.gather(*(one(cam_id, entry) for cam_id, entry in queue))
    return [item for item in done if item is not None]


def _previous_count(out_dir: Path) -> int:
    path = out_dir / "v2" / "manifest.json"
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("count", 0))
    except (ValueError, OSError):
        return 0
