"""Quality score, 0-100. Decides what makes the cut when the catalog is capped.

Weights favour what a viewer notices first: does it play, is it pretty, is it
sharp. Metadata completeness is a tie-breaker, not a driver.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .state import parse_iso, uptime

_THEME = {
    "marine": 20, "beaches": 20, "mountains": 20, "nature": 20,
    "cities": 16, "ports": 14, "landscapes": 14, "weather": 8,
}


def score(entry: dict[str, Any], now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    meta = entry["meta"]
    probe = entry.get("probe") or {}

    # Reliability (40): uptime over recent history, plus proven longevity.
    first_seen = parse_iso(entry["first_seen"] + "T00:00:00Z")
    age_days = (now - first_seen).days if first_seen else 0
    reliability = uptime(entry) * 30 + min(age_days, 30) / 30 * 10

    # Media (25): live video that every Tizen TV can decode beats a still.
    media = probe.get("media")
    if media == "hls" and probe.get("tizen_ok", True):
        playback = 20
    elif media in {"mp4", "dash", "youtube"}:
        playback = 16
    elif media == "image":
        playback = 8
    else:
        playback = 0
    height = probe.get("height") or 0
    sharpness = 5 if height >= 1080 else 3 if height >= 720 else 0

    theme = _THEME.get(meta.get("category", ""), 0)

    completeness = sum(
        (
            3 if meta.get("country") else 0,
            3 if meta.get("city") else 0,
            4 if "lat" in meta else 0,
            2 if meta.get("attribution") else 0,
            3 if meta.get("image") else 0,
        )
    )

    return round(reliability + playback + sharpness + theme + completeness)
