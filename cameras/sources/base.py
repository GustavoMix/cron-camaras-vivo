"""Base class and result type for camera sources.

A *source* is one upstream feed. Sources are intentionally independent: one
returning garbage, timing out, or changing its JSON shape must never stop the
others from producing data.
"""

from __future__ import annotations

import abc
import time
from dataclasses import dataclass, field
from typing import Any

from ..http import Http
from ..models import Camera


@dataclass(slots=True)
class SourceResult:
    """Outcome of running a single source, including failures.

    Failures are data, not exceptions: they are written to the health report so
    a feed that quietly dies is visible in the next run rather than silently
    shrinking the dataset.
    """

    source_id: str
    cameras: list[Camera] = field(default_factory=list)
    ok: bool = True
    error: str | None = None
    duration_s: float = 0.0
    # Records the feed returned but that failed validation (no media, bad URL).
    skipped: int = 0

    @property
    def count(self) -> int:
        return len(self.cameras)

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.source_id,
            "ok": self.ok,
            "cameras": self.count,
            "duration_s": round(self.duration_s, 2),
        }
        if self.skipped:
            payload["skipped"] = self.skipped
        if self.error:
            payload["error"] = self.error[:400]
        return payload


class Source(abc.ABC):
    """One upstream camera feed."""

    #: Stable slug. Appears in every camera id, so changing it re-keys the data.
    id: str = ""
    #: Human-readable publisher name, surfaced as attribution.
    name: str = ""
    #: ISO 3166-1 alpha-2 country, when the source covers exactly one.
    country: str | None = None
    #: Licence or terms-of-use note for the published data.
    license: str | None = None
    #: Page a human can visit to learn about the feed.
    homepage: str | None = None
    #: When set, the source is skipped unless this environment variable is set.
    requires_env: str | None = None

    @abc.abstractmethod
    async def fetch(self, http: Http) -> list[Camera]:
        """Return the cameras this source publishes.

        Implementations should tolerate unexpected shapes and drop bad records
        rather than raising; raising is reserved for a feed that is wholly
        unreachable.
        """

    async def run(self, http: Http) -> SourceResult:
        """Execute :meth:`fetch`, converting any failure into a result object."""
        started = time.monotonic()
        try:
            cameras = await self.fetch(http)
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
            return SourceResult(
                source_id=self.id,
                ok=False,
                error=f"{type(exc).__name__}: {exc}",
                duration_s=time.monotonic() - started,
            )
        return SourceResult(
            source_id=self.id,
            cameras=cameras,
            duration_s=time.monotonic() - started,
        )

    def describe(self) -> dict[str, Any]:
        """Catalog entry published alongside the data."""
        return {
            "id": self.id,
            "name": self.name,
            "country": self.country,
            "license": self.license,
            "homepage": self.homepage,
        }
