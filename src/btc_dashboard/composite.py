"""One reading per domain, gathered into a single card.

A digest, not an index. Given the snapshot it returns rows; the page and the
terminal both render those rows, so the two cannot come to disagree about what
the balance of evidence is. It collects nothing and re-fetches nothing — like
every other consumer, it is a pure function of the dict.

**There is no score and no label.** The obvious version of this card is a
weighted 0-100 with a name on it ("74/100, Quiet Accumulation"). Three things
are wrong with that and none of them are fixable by choosing better weights:

- *It cannot carry a qualifier.* Every figure in this tool states the window it
  was ranked against or the basis it was summed on, because that is what makes
  it comparable to someone else's. The honest qualifier on a 74 is "out of a
  scale invented here, comparable to nothing".
- *Half the inputs have no direction.* Realized volatility fires at both tails
  and carries no sign; trade count is participation, not direction; an RSI
  above 70 is a level, not a forecast — the sources say so themselves, and the
  PRICE card deliberately leaves RSI uncoloured for exactly this reason. Any
  weighted sum has to assign those a sign they do not have.
- *The components are not independent.* Trend and momentum are the same close
  series; exchange volume and trade count correlate 0.90 at this venue. Adding
  them as though they were independent makes the total swing further than the
  evidence does.

So the card states six readings side by side, each with its own window, and
leaves the weighing to the reader. If a score is ever wanted, the way in is a
study under `tools/` scoring it against the base rate first — the pattern
`hashrate_study.py` sets, whose headline finding is negative and worth keeping.

**A missing reading is `n/a` and still occupies its row.** The same rule as an
unfillable flow window or SMA: never a shorter basis wearing a longer label.
There is no renormalisation onto the components that did report, because five
readings averaged and presented as six is the defect this project keeps having.
The coverage count says how many of the rows carry a value, so an incomplete
card is legible as incomplete rather than as a weaker reading.

**Not in the snapshot, and not in the analyst's context.** Not in the snapshot
because it is derived rather than collected: putting it there would add a field
every ingested payload could carry — and a *score arriving from elsewhere* is
the one field where believing a stranger's arithmetic is invisible. Every
consumer here recomputes it from the sources instead. Not in the analyst's
context because it is a selection of facts already in that context: repeating
six of them would imply an emphasis the data has not earned, and a model shown
a digest reasons over the digest.
"""
from __future__ import annotations

import textwrap
from dataclasses import replace

from . import snapshot as snap
from .sources import Metric
from .text import safe_text

TITLE = "BALANCE OF EVIDENCE"

# What a row with nothing to report reads as. The literal is compared rather
# than a flag being carried, because the two ways a row gets here have nothing
# else in common: the source decided it (a window it cannot fill yet) or this
# module forced it (the source is down). `fmt`'s own default is this string, so
# a source writing its rows the ordinary way lands on it without trying.
NA = "n/a"

# Said on the card, not only here. It is what stops the thing drifting back
# into a score the first time someone wants a single number off it, and the
# direction sentence is load-bearing: half of these readings have no sign at
# all, so a reader totting up "good" and "bad" rows is doing arithmetic the
# measures do not support.
#
# It points at what the rows themselves say rather than naming them. `--only`
# builds a snapshot with two sources in it, and a note listing six domains then
# names rows that are not on the card. It cannot point at the *colour* either:
# strip the stylesheet and a note about which rows are coloured describes
# something invisible, while "marks events, not direction" is still printed on
# the row it belongs to.
NOTE = (
    "One reading per domain, each on its own window. Nothing is weighted, "
    "scored or combined into an index — the weighing is the reader's. Several "
    "of these readings state that they carry no direction; a high one is not "
    "a vote."
)


# Where the card's note wraps in the terminal. The rows themselves are left
# long — a reading and its window belong on one line, and the terminal's own
# wrapping is the least-bad way to break them — but the note is prose, and
# prose unwrapped beside six short rows reads as a wall rather than a caveat.
NOTE_WRAP = 78


def rows(snapshot: dict, *, reasons: bool = True) -> list[Metric]:
    """One row per domain, in source order.

    `reasons=False` keeps the `n/a` rows but drops the failure text from their
    notes, for the terminal's quiet mode. The row itself is never dropped: what
    `--quiet` suppresses is the *reason* a source is down, not the fact that a
    reading is missing — and a digest that silently shed a row there would be
    the renormalisation this module exists to refuse.

    Each source phrases its own rows, for the reason every other presentation
    lives beside its collector: the caveat a number needs belongs with the code
    that knows why it needs it. A digest restating "√365" or "Farside Total
    basis" from memory would drift from the card underneath it.

    Source order rather than a hand-picked order, so the two price-derived
    readings sit together — which is the honest way to show that trend and
    momentum are not two independent votes.
    """
    out: list[Metric] = []
    for name in snap.ordered_names(snapshot):
        block = snapshot["sources"][name]
        mod = snap.module_for(name)
        if mod is None or not hasattr(mod, "balance_rows"):
            continue
        try:
            # `data` is None for an unavailable source, and every source's
            # `balance_rows` is required to survive an empty dict and return
            # its labels anyway (see the contract in `sources/__init__.py`).
            # That is what lets a dead source still occupy its rows instead of
            # silently shrinking the card.
            got = list(mod.balance_rows(block.get("data") or {}) or [])
        except Exception:
            # A digest must never cost the page, the same rule `_notable`
            # follows. The row goes missing, which is visible in the coverage
            # count beside the title.
            continue
        if block.get("available"):
            out.extend(got)
            continue
        # Forced rather than trusted: an unavailable source's rows are `n/a` by
        # construction, and the reason it gave is more useful in the note than
        # whatever its formatters produced from an empty payload.
        #
        # The reason lands on the source's first row only, the same rule the
        # cards follow: one warehouse that is missing owns two rows here, and
        # the same sentence under both reads as two problems rather than one.
        # The rows of a source are contiguous, so the one carrying it is the
        # one directly above the others.
        reason = (safe_text(block.get("error") or "unavailable") if reasons
                  else "unavailable")
        out.extend(
            replace(r, value=NA, note=reason if i == 0 else None,
                    category=None, tone=None, note_tone=None)
            for i, r in enumerate(got)
        )
    return out


def coverage(metrics: list[Metric]) -> tuple[int, int]:
    """`(readings carrying a value, rows on the card)`."""
    return sum(1 for m in metrics if m.value != NA), len(metrics)


def complete(metrics: list[Metric]) -> bool:
    reported, total = coverage(metrics)
    return reported == total


def coverage_label(metrics: list[Metric]) -> str:
    reported, total = coverage(metrics)
    return f"{reported} of {total} readings"


def lines(metrics: list[Metric]) -> list[str]:
    """The card as terminal text.

    The category goes in a column before the value, where a reader meets it
    first — the whole reason it was promoted out of the note. Blank for the
    rows whose measure defines no classifier, which is what the card has to
    say about them.

    Dot leaders rather than spaces: the value column is ragged — a percentage,
    a percentile, a dollar figure — and over six rows a run of spaces stops
    connecting a label to the number opposite it.

    The note rides the same line because there is nowhere else, which is the
    same compromise `Metric` documents for the terminal generally. It is not
    optional: the window is what makes the reading comparable.

    The card's own note is wrapped into separate list entries rather than
    returned as one long string carrying newlines. `render()` indents only the
    first physical line of a body string, so a string that wraps itself would
    put every line after the first at column 0 — which is the shape of a block
    heading, and the exact forgery `text.safe_text` exists to prevent.
    """
    if not metrics:
        return []
    width = max(len(m.label) for m in metrics)
    # A column of its own, padded even where it is empty, so the values still
    # line up underneath each other and the blanks read as blanks rather than
    # as a ragged left edge. Absent entirely when no row has a category.
    cat_width = max((len(m.category or "") for m in metrics), default=0)
    out = [
        f"{(m.label + ' ').ljust(width + 2, '.')} "
        + (f"{(m.category or '').ljust(cat_width)} " if cat_width else "")
        + m.value
        + (f" — {m.note}" if m.note else "")
        for m in metrics
    ]
    out.extend(textwrap.wrap(NOTE, NOTE_WRAP))
    return out
