"""Per-camera history: lets the cron remember, throttle and never lose cameras.

State machine::

    candidate --2 ok--> active --fail--> degraded --3 fails--> offline --30d--> retired
        ^                  ^____2 ok (recover)____________|          |
        |_________________ any ok (revived) ________________________|

Only ``active`` and ``degraded`` cameras are published; ``degraded`` keeps a
single bad probe from yanking a camera off the TV. Nothing is ever deleted.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

HISTORY_LEN = 28
PUBLISHED = frozenset({"active", "degraded"})
RETIRE_AFTER = timedelta(days=30)
# Cron jitter: GitHub fires scheduled runs minutes late, sometimes early.
SLACK = timedelta(minutes=45)


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0, tzinfo=None).isoformat() + "Z"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.rstrip("Z")).replace(tzinfo=UTC)


def new_entry(meta: dict[str, Any], now: datetime) -> dict[str, Any]:
    return {
        "meta": meta,
        "status": "candidate",
        "first_seen": iso(now)[:10],
        "last_seen": iso(now)[:10],
        "last_checked": None,
        "last_ok": None,
        "next_check": None,
        "ok_streak": 0,
        "fail_streak": 0,
        "history": "",
        "probe": None,
    }


def uptime(entry: dict[str, Any]) -> float:
    history = entry.get("history", "")
    return history.count("o") / len(history) if history else 0.0


def interval(entry: dict[str, Any]) -> timedelta:
    """How long to leave a camera alone before probing it again."""
    status = entry["status"]
    if status == "retired":
        return timedelta(days=7)
    if status == "offline":
        return timedelta(hours=48)
    if status == "active" and len(entry["history"]) >= 6 and uptime(entry) >= 0.95:
        return timedelta(hours=24)  # proven stable: half the probes
    return timedelta(0)  # candidate / degraded / young: every run


def is_due(entry: dict[str, Any], now: datetime) -> bool:
    due = parse_iso(entry.get("next_check"))
    return due is None or now + SLACK >= due


def apply_probe(entry: dict[str, Any], result: dict[str, Any], ok: bool, now: datetime) -> None:
    """Fold one probe outcome into the entry (mutates it)."""
    entry["last_checked"] = iso(now)
    entry["probe"] = result
    entry["history"] = (entry["history"] + ("o" if ok else "x"))[-HISTORY_LEN:]

    if ok:
        entry["ok_streak"] += 1
        entry["fail_streak"] = 0
        entry["last_ok"] = iso(now)
        status = entry["status"]
        recovered = status in {"candidate", "offline"} and entry["ok_streak"] >= 2
        if recovered or status == "degraded":
            entry["status"] = "active"
        elif status == "retired":
            entry["status"] = "candidate"
    else:
        entry["fail_streak"] += 1
        entry["ok_streak"] = 0
        status = entry["status"]
        if status == "active":
            entry["status"] = "degraded"
        elif status in {"degraded", "candidate"} and entry["fail_streak"] >= 3:
            entry["status"] = "offline"
        if entry["status"] == "offline" and _offline_too_long(entry, now):
            entry["status"] = "retired"

    entry["next_check"] = iso(now + interval(entry))


def _offline_too_long(entry: dict[str, Any], now: datetime) -> bool:
    anchor = parse_iso(entry.get("last_ok")) or parse_iso(entry["first_seen"] + "T00:00:00Z")
    return anchor is not None and now - anchor > RETIRE_AFTER


class StateStore:
    def __init__(self, entries: dict[str, dict[str, Any]] | None = None) -> None:
        self.entries: dict[str, dict[str, Any]] = entries or {}

    @classmethod
    def load(cls, path: Path) -> StateStore:
        if not path.exists():
            return cls()
        return cls(json.loads(path.read_text(encoding="utf-8")).get("cameras", {}))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema": 1, "cameras": dict(sorted(self.entries.items()))}
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        path.write_text(text + "\n", encoding="utf-8")
