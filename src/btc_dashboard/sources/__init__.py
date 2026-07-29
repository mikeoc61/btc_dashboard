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


def fmt(value, spec: str = "", *, prefix: str = "", suffix: str = "",
        missing: str = "n/a") -> str:
    """Format a possibly-missing value without ever raising.

    Renderers run over data that may be partial: a source can legitimately
    return some fields and not others, and an *ingested* snapshot may carry a
    field of the wrong type entirely. A bare f-string blows up on both, and
    because a raise costs the whole block, one missing number takes out an
    entire section of the panel. Everything numeric goes through here.
    """
    if value is None:
        return missing
    try:
        return f"{prefix}{value:{spec}}{suffix}"
    except (TypeError, ValueError):
        return missing
