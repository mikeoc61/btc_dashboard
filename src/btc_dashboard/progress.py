"""A liveness cue while the analyst is working.

`--ask` blocks with no output at all once the panel has printed, and it can
block for a long time: the analyst may run up to `providers.MAX_TOOL_ROUNDS`
paid rounds before it answers, each a network round trip plus a query. A
terminal that has stopped printing is indistinguishable from one that has hung,
so this paints a spinner and an elapsed-seconds counter on one line and erases
it when the call returns.

The counter is the part that does the work. A static word cannot be told apart
from the frozen terminal it exists to rule out; a number going up can.

Not in `render.py`, which promises to be a pure function of the snapshot —
same dict, same text — and this is a thread that paints the clock.

It writes to **stderr**, never stdout. stdout carries the panel and the
answer and is routinely redirected or piped; stderr is where the `QUERIED`
lines and the token count already go, and it is still the terminal when stdout
is a file. Off a terminal it paints nothing at all: unlike a REPL, whose log
wants a marker for an operation that took a minute, this is a one-shot command
whose stderr is read by whatever captured it.
"""
from __future__ import annotations

import itertools
import os
import sys
import threading
import time

from .render import DIM, RESET, supports_color

# The same four characters the page's spinner uses, for the same reason and
# stated in both places because the terminal and the page are separate
# presentations: a nicer glyph is font-dependent, and a terminal whose font
# lacks it draws a substitution box or nothing, leaving the counter beside it
# to carry the whole message on its own.
FRAMES = ("|", "/", "-", "\\")
# Seconds between frames. Ten a second is smooth without being a source of
# wakeups worth thinking about.
FRAME_SECONDS = 0.1
# Hold off this long before drawing anything. An ask that returns quickly then
# never paints at all, rather than flashing a spinner onto the screen and
# wiping it — which reads as a glitch, not as progress.
PAINT_AFTER = 1.5


class Activity:
    """Show that something is happening, for the duration of a `with` block.

    On a terminal: a daemon thread paints `<frame> <label>... <n>s` on one
    line until `__exit__`, which stops it, joins it, and erases the line so the
    answer starts on a clean one. Anywhere else: nothing.

    The thread writes only while the calling thread is parked inside the
    blocking call, and it is stopped and joined *before* the line is erased, so
    there is no window in which both write.
    """

    def __init__(self, label: str, *, stream=None, color: bool | None = None):
        self.label = label
        self._stream = sys.stderr if stream is None else stream
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._painted = False
        # A terminal is the only place a carriage return means anything. TERM
        # =dumb says so explicitly even when the stream is a tty.
        self._animate = (
            bool(getattr(self._stream, "isatty", lambda: False)())
            and os.environ.get("TERM") != "dumb"
        )
        # Dimming follows --color, like every other painted string; it is the
        # spinner's styling, not the spinner itself, so `--color never` still
        # gets the cue. Decided against *this* stream: stdout can be a pipe
        # while stderr is still the terminal.
        self._dim = supports_color(self._stream) if color is None else color

    def __enter__(self) -> "Activity":
        if not self._animate:
            return self
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def _run(self) -> None:
        start = time.monotonic()
        if self._stop.wait(PAINT_AFTER):
            return                      # finished before it was worth saying
        self._painted = True
        for frame in itertools.cycle(FRAMES):
            if self._stop.is_set():
                break
            line = f"{frame} {self.label}… {int(time.monotonic() - start)}s"
            self._write("\r" + (f"{DIM}{line}{RESET}" if self._dim else line))
            self._stop.wait(FRAME_SECONDS)

    def __exit__(self, *exc) -> None:
        if self._thread is None:
            return
        self._stop.set()
        self._thread.join(timeout=1.0)
        if self._painted:
            # Carriage return and erase to end of line, so nothing of the
            # spinner survives beside the answer — including on the exception
            # path, where the traceback would otherwise start mid-line.
            self._write("\r\033[K")

    def _write(self, text: str) -> None:
        # A closed or broken stream must never cost the answer that is on its
        # way: this is a cue, and the call it is decorating has already been
        # paid for.
        try:
            self._stream.write(text)
            self._stream.flush()
        except (ValueError, OSError):
            self._stop.set()
