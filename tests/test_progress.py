"""The liveness cue during a blocking analyst call.

`--ask` prints the panel and then goes silent for as long as the analyst takes,
which with tool rounds can be minutes. Silence and a hang look identical from
the other side of the screen.
"""
from __future__ import annotations

import io
import time

import pytest

from btc_dashboard import progress


class _Tty(io.StringIO):
    """A stream that claims to be a terminal, which is the only place a
    carriage return means anything."""

    def isatty(self) -> bool:
        return True


def _quick(monkeypatch, seconds: float = 0.0) -> None:
    """Remove the hold-off so a test does not have to wait it out."""
    monkeypatch.setattr(progress, "PAINT_AFTER", seconds)


def _settle(stream, want: str = "", tries: int = 100) -> str:
    """Give the painter thread a moment; it runs on its own clock."""
    for _ in range(tries):
        if want in stream.getvalue() and stream.getvalue():
            break
        time.sleep(0.01)
    return stream.getvalue()


class TestItOnlySpeaksToATerminal:
    def test_a_pipe_gets_nothing(self, monkeypatch):
        """stderr is captured by whatever ran the command. A spinner redrawing
        itself forty times a second is not something a log wants, and a
        carriage return there is not progress, it is corruption."""
        _quick(monkeypatch)
        out = io.StringIO()
        with progress.Activity("thinking", stream=out):
            time.sleep(0.1)
        assert out.getvalue() == ""

    def test_a_dumb_terminal_gets_nothing(self, monkeypatch):
        """It says it cannot do this, so it is believed."""
        _quick(monkeypatch)
        monkeypatch.setenv("TERM", "dumb")
        out = _Tty()
        with progress.Activity("thinking", stream=out):
            time.sleep(0.1)
        assert out.getvalue() == ""

    def test_a_terminal_gets_the_label(self, monkeypatch):
        _quick(monkeypatch)
        out = _Tty()
        with progress.Activity("thinking", stream=out):
            _settle(out, "thinking")
        assert "thinking" in out.getvalue()


class TestTheCounterIsThePoint:
    def test_the_elapsed_seconds_are_painted(self, monkeypatch):
        """A static word cannot be told apart from the frozen terminal it
        exists to rule out. A number going up can."""
        _quick(monkeypatch)
        out = _Tty()
        with progress.Activity("thinking", stream=out):
            _settle(out, "0s")
        assert "0s" in out.getvalue()

    def test_the_frames_are_ascii(self):
        """A braille or block spinner is font-dependent, and a terminal whose
        font lacks it draws nothing at all — leaving the counter to carry the
        whole message."""
        assert all(ord(c) < 128 for frame in progress.FRAMES for c in frame)

    def test_it_keeps_painting(self, monkeypatch):
        """One frame is a message that arrived; a sequence is a process that is
        still running."""
        _quick(monkeypatch)
        out = _Tty()
        with progress.Activity("thinking", stream=out):
            for _ in range(200):
                if out.getvalue().count("\r") > 2:
                    break
                time.sleep(0.01)
        assert out.getvalue().count("\r") > 2


class TestItLeavesNothingBehind:
    def test_the_line_is_erased_on_the_way_out(self, monkeypatch):
        """The answer is printed straight after this block and must start on a
        clean line, not beside a half-drawn spinner."""
        _quick(monkeypatch)
        out = _Tty()
        with progress.Activity("thinking", stream=out):
            _settle(out, "thinking")
        assert out.getvalue().endswith("\r\033[K")

    def test_an_exception_still_clears_it(self, monkeypatch):
        """Ctrl-C during a long ask is the realistic case, and a traceback
        starting mid-spinner is unreadable."""
        _quick(monkeypatch)
        out = _Tty()
        with pytest.raises(KeyboardInterrupt):
            with progress.Activity("thinking", stream=out):
                _settle(out, "thinking")
                raise KeyboardInterrupt
        assert out.getvalue().endswith("\r\033[K")

    def test_a_fast_call_never_paints_at_all(self, monkeypatch):
        """Held off rather than drawn and wiped: a spinner that appears for
        80ms reads as a glitch, not as progress. Nothing is written, so there
        is nothing to erase either."""
        monkeypatch.setattr(progress, "PAINT_AFTER", 30.0)
        out = _Tty()
        with progress.Activity("thinking", stream=out):
            pass
        assert out.getvalue() == ""

    def test_a_broken_stream_does_not_cost_the_answer(self, monkeypatch):
        """The call this decorates has already been paid for. A cue that cannot
        be drawn is not a reason to lose it."""
        _quick(monkeypatch)

        class Broken(_Tty):
            def write(self, text):
                raise OSError("gone")

        with progress.Activity("thinking", stream=Broken()):
            time.sleep(0.1)


class TestColour:
    def test_dimming_follows_the_colour_switch(self, monkeypatch):
        """`--color never` reaches it like every other painted string."""
        _quick(monkeypatch)
        out = _Tty()
        with progress.Activity("thinking", stream=out, color=False):
            _settle(out, "thinking")
        assert "\033[2m" not in out.getvalue()

    def test_colour_on_is_dimmed(self, monkeypatch):
        _quick(monkeypatch)
        out = _Tty()
        with progress.Activity("thinking", stream=out, color=True):
            _settle(out, "thinking")
        assert "\033[2m" in out.getvalue()

    def test_the_cue_itself_survives_no_colour(self, monkeypatch):
        """Colour is the styling, not the message — the same rule the rest of
        the output follows."""
        _quick(monkeypatch)
        out = _Tty()
        with progress.Activity("thinking", stream=out, color=False):
            _settle(out, "thinking")
        assert "thinking" in out.getvalue()


class TestItStaysOffStdout:
    """stdout carries the panel and the answer, and is routinely redirected or
    piped. A carriage return in there does not scroll past — it overwrites the
    line someone kept.
    """

    def _origin(self, tmp_path):
        import json

        payload = {
            "schema_version": 1, "generated_at": "2026-08-28T03:00:00+00:00",
            "asset": "btc",
            "sources": {"price": {
                "available": True, "stale": False, "cached": False,
                "cache_age_seconds": None, "as_of": None, "error": None,
                "data": {"spot": 63423.0, "source": "coingecko"},
            }},
        }
        path = tmp_path / "snap.json"
        path.write_text(json.dumps(payload))
        return str(path)

    def test_the_answer_is_untouched_and_nothing_leaks(self, tmp_path,
                                                       monkeypatch, capsys):
        from btc_dashboard import analyst, cli

        monkeypatch.setattr(analyst, "ask", lambda *a, **kw: analyst.AnalystResult(
            text="Flows and price agree.", provider="anthropic", model="m",
            input_tokens=1, output_tokens=2,
        ))
        assert cli.main([
            "--from", self._origin(tmp_path), "--ask", "q", "--color", "never"
        ]) == 0

        out = capsys.readouterr()
        assert "Flows and price agree." in out.out
        assert "\r" not in out.out
        assert "\033[K" not in out.out and "thinking" not in out.out

    def test_a_captured_run_is_clean_on_both_streams(self, tmp_path,
                                                     monkeypatch, capsys):
        """Under capture neither stream is a terminal, so the cue is never
        drawn at all — which is what a redirected run should look like."""
        from btc_dashboard import analyst, cli

        monkeypatch.setattr(analyst, "ask", lambda *a, **kw: analyst.AnalystResult(
            text="ok", provider="anthropic", model="m",
            input_tokens=1, output_tokens=2,
        ))
        cli.main(["--from", self._origin(tmp_path), "--ask", "q",
                  "--color", "never"])
        out = capsys.readouterr()
        assert "\033[K" not in out.err and "\r" not in out.err
