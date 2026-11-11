"""Rights gate. Default-deny: nothing is published without recorded evidence.

A source (or an individual hand-curated camera) is publishable only when ALL
of these hold:

* status is ``verified``
* ``commercial_use`` is exactly ``true``
* an ``evidence_url`` points at the terms/licence that was read
* ``verified_on`` is a real date and ``review_by`` (if set) is not past

Licences get re-reviewed on a schedule because terms change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

PUBLISHABLE = "verified"


@dataclass(slots=True)
class Verdict:
    ok: bool
    reason: str = "ok"
    license: str | None = None
    attribution: str | None = None
    evidence_url: str | None = None
    verified_on: str | None = None


class RightsPolicy:
    def __init__(self, sources: dict[str, dict[str, Any]]) -> None:
        self.sources = sources

    @classmethod
    def load(cls, path: Path) -> RightsPolicy:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return cls(payload.get("sources", {}))

    def status(self, source_id: str) -> str:
        return str(self.sources.get(source_id, {}).get("status", "unknown"))

    def source_ids(self, *statuses: str) -> set[str]:
        return {sid for sid in self.sources if self.status(sid) in statuses}

    def verdict(self, source_id: str, rights: dict[str, Any] | None, today: date) -> Verdict:
        """Evaluate a camera. ``rights`` overrides the source policy when given."""
        if rights is not None:
            record = {"status": PUBLISHABLE, **rights}
        else:
            record = self.sources.get(source_id)
            if record is None:
                return Verdict(False, "unknown_source")

        if record.get("status") != PUBLISHABLE:
            return Verdict(False, f"status_{record.get('status', 'missing')}")
        if record.get("commercial_use") is not True:
            return Verdict(False, "commercial_use_not_confirmed")
        if not record.get("evidence_url"):
            return Verdict(False, "no_evidence")
        verified_on = _parse_date(record.get("verified_on"))
        if verified_on is None:
            return Verdict(False, "no_verification_date")
        review_by = _parse_date(record.get("review_by"))
        if record.get("review_by") and review_by is None:
            return Verdict(False, "bad_review_date")
        if review_by is not None and review_by < today:
            return Verdict(False, "review_overdue")

        return Verdict(
            True,
            license=record.get("license"),
            attribution=record.get("attribution"),
            evidence_url=record["evidence_url"],
            verified_on=verified_on.isoformat(),
        )


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None
