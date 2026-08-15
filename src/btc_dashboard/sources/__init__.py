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

from dataclasses import dataclass, field, replace
from typing import Any


@dataclass(frozen=True)
class SourceResult:
    """One source's contribution to the snapshot.

    `data` is only meaningful when `available` is True.

    `cached` and `stale` are distinct and both can appear:

    - `cached` — served from disk within its TTL. As good as when it was
      fetched; `cache_age_seconds` says how long ago that was.
    - `stale` — served from disk *after the live path failed*, with the TTL
      already expired. Available, but older than it looks.

    Stale implies cached; cached does not imply stale.
    """

    name: str
    available: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    stale: bool = False
    as_of: str | None = None
    cached: bool = False
    cache_age_seconds: int | None = None
    cache_ttl_seconds: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "stale": self.stale,
            "cached": self.cached,
            "cache_age_seconds": self.cache_age_seconds,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "as_of": self.as_of,
            "error": self.error,
            "data": self.data if self.available else None,
        }

    def as_cached(self, age_seconds: float, ttl_seconds: int) -> "SourceResult":
        """Same data, marked as served from a within-TTL cache."""
        return replace(
            self,
            cached=True,
            stale=False,
            cache_age_seconds=int(age_seconds),
            cache_ttl_seconds=ttl_seconds,
        )

    def as_stale(self, age_seconds: float, error: str | None) -> "SourceResult":
        """Same data, marked as an expired copy serving after a failed refresh.

        The live failure's error is carried through so the reason is visible
        rather than being replaced by silence.
        """
        return replace(
            self,
            cached=True,
            stale=True,
            cache_age_seconds=int(age_seconds) if age_seconds != float("inf") else None,
            error=error,
        )


def unavailable(name: str, error: str) -> SourceResult:
    return SourceResult(name=name, available=False, error=error)


@dataclass(frozen=True)
class Metric:
    """One labelled reading, for a layout that has rows rather than lines.

    `note` is where the qualifier goes — the window a percentile was ranked
    against, the annualisation of a volatility figure, the noise band on a
    single day's count. In the terminal these ride on the same line because
    there is nowhere else; a page has room to put them under the value, and
    dropping them there would be a regression dressed up as a cleaner design.
    """

    label: str
    value: str
    note: str | None = None
    # Presentational hint only: "up", "down", "warn". Never the sole carrier
    # of meaning — the text must read correctly with all styling removed.
    tone: str | None = None


@dataclass(frozen=True)
class Panel:
    """A titled group of metrics — one card in a grid.

    Sources emit their own grouping rather than the page imposing one, so a
    source that naturally splits (facts, signals, volatility) says so itself.
    """

    title: str
    metrics: list[Metric]


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
