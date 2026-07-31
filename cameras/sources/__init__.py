"""Registry of every camera source.

Adding a feed means writing a module here that exposes ``build_sources()`` and
listing it in :data:`_MODULES`. Nothing else in the pipeline needs to change.
"""

from __future__ import annotations

import os

from . import caltrans, castlerock, nyctmc, qldtraffic, tfl, windy, wsdot
from .base import Source, SourceResult

_MODULES = (castlerock, caltrans, nyctmc, tfl, windy, wsdot, qldtraffic)

__all__ = ["Source", "SourceResult", "all_sources", "select_sources"]


def all_sources() -> list[Source]:
    """Every registered source, ordered deterministically by id."""
    sources: list[Source] = []
    for module in _MODULES:
        sources.extend(module.build_sources())

    seen: set[str] = set()
    for source in sources:
        if source.id in seen:
            raise ValueError(f"duplicate source id: {source.id}")
        seen.add(source.id)

    return sorted(sources, key=lambda s: s.id)


def select_sources(only: list[str] | None = None, *, skip_gated: bool = True) -> list[Source]:
    """Resolve the sources to run for this invocation.

    ``only`` restricts the run to specific ids (useful when developing an
    adapter). Sources gated behind a missing credential are dropped by default
    so they do not fill the health report with predictable auth failures.
    """
    sources = all_sources()

    if only:
        wanted = {name.strip().lower() for name in only if name.strip()}
        known = {source.id for source in sources}
        unknown = wanted - known
        if unknown:
            raise ValueError(
                f"unknown source(s): {', '.join(sorted(unknown))}. "
                f"Known: {', '.join(sorted(known))}"
            )
        sources = [source for source in sources if source.id in wanted]

    if skip_gated:
        sources = [
            source
            for source in sources
            if not source.requires_env or os.environ.get(source.requires_env, "").strip()
        ]

    return sources
