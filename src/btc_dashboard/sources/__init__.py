"""Data sources.

Every source is a module exposing the same four names:

    NAME            str — key it occupies in the snapshot
    collect(cfg)    -> SourceResult, never raises
    render_lines(d) -> list[str], CLI text for that block
    context_lines(d)-> list[str], facts phrased for the LLM

Colocating the two presentations with the collector means adding a source is a
single new file plus one entry in `snapshot.SOURCES` — nothing else changes.

`collect` never raising is the load-bearing property: an unreachable node, a
missing warehouse, or a Farside layout change degrades one block and leaves the
rest of the snapshot intact. A source that cannot produce data returns
`unavailable(...)` with the reason attached, which is a fact the reader (and the
analyst) gets to see rather than a silent gap.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class SourceResult:
    """One source's contribution to the snapshot.

    `data` is only meaningful when `available` is True. `stale` marks data
    served from cache after a live fetch failed — available, but older than it
    looks, so consumers can flag it rather than treating it as current.
    """

    name: str
    available: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    stale: bool = False
    as_of: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "stale": self.stale,
            "as_of": self.as_of,
            "error": self.error,
            "data": self.data if self.available else None,
        }


def unavailable(name: str, error: str) -> SourceResult:
    return SourceResult(name=name, available=False, error=error)
