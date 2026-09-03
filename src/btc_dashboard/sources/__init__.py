"""Data sources.

Every source is a module exposing the same four names:

    NAME            str — key it occupies in the snapshot
    collect(cfg)    -> SourceResult, never raises
    render_lines(d) -> list[str], CLI text for that block
    context_lines(d)-> list[str], facts phrased for the LLM

and optionally `analyst_tools(cfg) -> list[Tool]`, for a source that can answer
questions the snapshot does not contain (see `Tool`), plus
`analyst_scope(data) -> str | None`, one line saying what that tool can reach —
rendered where a question is composed, so the reader knows what is answerable
before asking rather than after.

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
from typing import Any, Callable

from ..text import safe_text


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

    Both are about *provenance* — where this payload came from — and never
    about whether its contents are current. A source whose upstream data has
    fallen behind is not a stale cache, and must not borrow this flag to say
    so: `warehouse` did, to earn a badge, and the analyst was consequently told
    "the live refresh failed, so these figures come from a cache" about a read
    that had just succeeded. Content facts travel in `data`, and a source that
    wants one on the page says so through `notable()`.
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
    # Presentational hints only: "up", "down", "warn". Never the sole carrier
    # of meaning — the text must read correctly with all styling removed.
    #
    # Two of them, because the colour has to land on the thing it describes.
    # A 20-day SMA of $63,955 painted red because spot sits below it reads as
    # "the average fell"; what is negative is the *relationship*, and the
    # relationship lives in the note. `tone` colours the value, `note_tone`
    # colours the note, and a row uses whichever one is actually signed.
    tone: str | None = None
    note_tone: str | None = None


@dataclass(frozen=True)
class Panel:
    """A titled group of metrics — one card in a grid.

    Sources emit their own grouping rather than the page imposing one, so a
    source that naturally splits (facts, signals, volatility) says so itself.
    """

    title: str
    metrics: list[Metric]
    # A qualifier that belongs to several rows rather than to one. Use it only
    # when repeating it per row would say the same thing three times — a shared
    # venue, a shared as-of day. A qualifier that applies to one reading stays
    # in that reading's own `note`, where it cannot be separated from it, and a
    # qualifier the whole card shares belongs in the title. Like `Metric.note`,
    # it must read correctly with all styling removed.
    note: str | None = None
    # Where this card sits on the page, lowest first. A source declares its
    # own placement because the useful order is not source order: volatility
    # belongs beside price, since a distance from a moving average only means
    # something in volatility units, while both come from different sources.
    # Ties keep source order, so the layout stays stable.
    priority: int = 50


@dataclass(frozen=True)
class Tool:
    """A live capability a source lends the analyst, used only while answering.

    The snapshot is a fixed set of derived figures chosen in advance. Some
    questions are not in it and never could be — "how did this drawdown compare
    to the last three", "what did volatility do around the 2024 halving" — and
    a source that can still answer at ask time says so with one of these.

    This is the single documented exception to *nothing downstream re-fetches*,
    and it is deliberately narrow. A tool answers the model's question and
    nothing else: it does not collect, its result never enters the snapshot,
    and no other consumer can reach it. The snapshot a page renders is still
    the snapshot the analyst was handed.

    `run` receives the model's arguments as keywords and returns text for it to
    read. It must never raise: a bad argument is something the model can see
    and correct on the next turn, so the failure is phrased for the model and
    returned like any other result. Anything it cannot phrase, it says.
    """

    name: str
    description: str
    # JSON Schema for the arguments, as both wire protocols expect.
    parameters: dict[str, Any]
    run: Callable[..., str]


def fmt(value, spec: str = "", *, prefix: str = "", suffix: str = "",
        missing: str = "n/a") -> str:
    """Format a possibly-missing value without ever raising, on one line.

    Renderers run over data that may be partial: a source can legitimately
    return some fields and not others, and an *ingested* snapshot may carry a
    field of the wrong type entirely. A bare f-string blows up on both, and
    because a raise costs the whole block, one missing number takes out an
    entire section of the panel. Everything numeric goes through here.

    Which is why the bounding goes here too. A format spec rejects a string
    already — `fmt("x", ".0f")` is `n/a` — but with no spec the value was
    reproduced verbatim, newlines and all. That was enough for an ingested
    snapshot to put a line beginning "[SYSTEM]" at column 0 of the analyst's
    context block, where it reads as a section header rather than as the value
    of a field, and to fake a row in the terminal panel the same way. The HTML
    page escapes everything, so it was never exposed; the other two consumers
    were.

    The bounding itself lives in `text.safe_text`, because `fmt` is not the
    only way a snapshot field reaches a line — a categorical value like
    `regime` or a source's own name is interpolated directly — and one rule
    stated once is easier to keep true than the same rule written twice.

    No call site formats with an alignment or padding spec, so collapsing runs
    of whitespace cannot disturb a layout.
    """
    if value is None:
        return missing
    try:
        text = f"{value:{spec}}"
    except (TypeError, ValueError):
        return missing
    return f"{prefix}{safe_text(text)}{suffix}"
