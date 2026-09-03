"""Bounding untrusted text before it can become a line of output.

Every figure this tool prints is worded by this code; only the *values* come
from the snapshot. That distinction is what a reader relies on, and it holds
only while a value stays inside the line it belongs to.

An ingested snapshot breaks that on its own. `--from` accepts a payload from a
URL or a file, so `error`, `as_of`, `regime` and a source's own name are
attacker-controlled text on its way to a terminal and to a prompt. Three
things let such a field stop being a field:

- **A newline** ends the line early and starts one at column 0, where the
  renderer's two-space body indent no longer applies. That is enough to forge
  a whole dashboard block, header and all.
- **An escape sequence** does worse than forge text: `ESC [ 2 J` clears the
  screen, and SGR codes repaint arbitrary parts of it. Collapsing whitespace
  never caught these, because `\\x1b` is not whitespace.
- **A bidi override** (`U+202E` and friends) reorders a line visually without
  changing a character in it, so what the terminal shows and what the string
  says are different documents.

`safe_text` closes all three, and is the one place that does, so the rule can
be stated once instead of re-derived at each call site.

Collapsed and stripped, never censored: the text is still the field's value
and the reader should see what was there. Removing the `ESC` byte leaves its
`[31m` payload visible as ordinary characters, which makes the tampering
legible rather than silent.
"""
from __future__ import annotations

import re

# A rendered value is one scalar on one line. Longer or multi-line is not a
# reading; it is a field carrying something that was never a measurement.
MAX_VALUE_CHARS = 120

# What survives whitespace collapsing and still isn't a printable character.
#
# C0/DEL/C1 covers ESC and every other control byte. The whitespace members of
# that range (\t \n \r \v \f, and the separators) are already spaces by the
# time this runs, so listing the range whole costs nothing and leaves no gap.
#
# The second row is the invisible-formatting set: zero-width characters and
# the bidi overrides. These are neither control codes nor whitespace, so
# nothing else here would catch them, and U+202E alone is enough to make a
# line read backwards from what it contains.
_UNSAFE = re.compile(
    "[\x00-\x1f\x7f-\x9f"
    "\u061c\u200b-\u200f\u202a-\u202e\u2066-\u2069]"
)


def safe_text(value, *, limit: int = MAX_VALUE_CHARS) -> str:
    """One line, printable, bounded — whatever `value` was.

    Order matters. Whitespace collapses first so a newline becomes a word gap
    rather than vanishing and running two words together; what remains after
    that is control and formatting characters with no reading to preserve, so
    they are dropped. The cap applies last, to the text that will actually be
    shown.

    Truncation is marked rather than silent: a value that got cut is itself
    worth seeing.
    """
    text = _UNSAFE.sub("", " ".join(str(value).split()))
    if len(text) > limit:
        text = text[:limit] + "…(truncated)"
    return text
