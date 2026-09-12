"""The readings extreme enough to lead with, gathered from the sources.

Shared, because two consumers need the same list and must not disagree about
it: the page brackets it into the balance card's heading, and the analyst is
told which readings crossed the bar. A reader who can see both on one screen —
the bracket sits directly above the ask box — would have no way to tell which
was wrong if they came from separate code.

Each source owns its own bounds through `notable()`, because what counts as
extreme is a property of the measure rather than of the page. Availability and
staleness are added here instead, being facts about the snapshot rather than
about any one source.

Every entry is a stated reading with its window attached — never an
interpretation. "30d volatility 22% — <1 pctile of 2y" is a fact; "compression,
expect a large move" is a forecast, and volatility carries no direction. The
reader draws the conclusion, and a test asserts the forecast words never
appear.
"""
from __future__ import annotations

from . import snapshot as snap
from .render import human_age
from .text import safe_text


def _label(name: str) -> str:
    """A source's name for an entry, bounded when the payload supplied it.

    A known source is named from `TITLES`, a constant in this build. A name
    this build has no title for came from an *ingested* snapshot, so it is
    attacker-controlled text heading for a terminal and a prompt; it is quoted
    for the same reason `analyst._quote_untrusted` quotes one — the prompt's
    trust boundary is the quotation mark, and an unquoted name is a boundary
    the model cannot see.
    """
    title = snap.TITLES.get(name)
    if title:
        return title.split(" (")[0].lower()
    return f'"{safe_text(name)}"'


def entries(snapshot: dict) -> list[str]:
    """Every notable reading, one bounded line each.

    Bounded here rather than at each consumer. The page escapes what it
    renders, but the prompt does not escape anything, and a source's own name
    reaches this list without ever passing through `fmt` — so a newline in an
    ingested payload's key could start a line at column 0 of the analyst's
    context, which is the shape of a section heading. One rule, stated once,
    for both consumers.
    """
    out: list[str] = []
    for name in snap.ordered_names(snapshot):
        block = snapshot["sources"][name]
        label = _label(name)
        if not block.get("available"):
            out.append(f"{label} unavailable")
            continue
        if block.get("stale"):
            age = block.get("cache_age_seconds")
            out.append(
                f"{label} is stale"
                + (f" ({human_age(age)} old)" if age is not None else "")
            )
        mod = snap.module_for(name)
        if mod is None or not hasattr(mod, "notable"):
            continue
        try:
            out.extend(mod.notable(block["data"]) or [])
        except Exception:
            # A threshold check must never cost the page.
            continue
    return [safe_text(line) for line in out]
