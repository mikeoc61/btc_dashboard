"""The HTML page: escaping, qualifiers, and graceful degradation."""
from __future__ import annotations

import pytest

from btc_dashboard import html as page
from btc_dashboard.sources import Metric, Panel


def _snap(**over):
    block = {
        "available": True, "stale": False, "cached": False,
        "cache_age_seconds": None, "cache_ttl_seconds": None,
        "as_of": None, "error": None,
        "data": {"spot": 63423.0, "source": "coingecko", "sma200": 69874.0,
                 "sma200_pct": -9.1, "sma200_position": "below",
                 "days_available": 201},
    }
    block.update(over)
    return {"schema_version": 1, "generated_at": "2026-08-15T01:53:00+00:00",
            "asset": "btc", "sources": {"price": block}}


class TestStructure:
    def test_is_self_contained(self):
        out = page.render_html(_snap())
        assert "<style>" in out
        for external in ("<script src", "<link rel=\"stylesheet\"", "http://", "https://"):
            assert external not in out, f"page must not reference {external}"

    def test_renders_values_and_labels(self):
        out = page.render_html(_snap())
        assert "$63,423" in out and "PRICE" in out and "200D SMA" in out

    def test_refresh_is_optional(self):
        assert "http-equiv=\"refresh\"" in page.render_html(_snap())
        assert "http-equiv=\"refresh\"" not in page.render_html(_snap(), refresh=None)


class TestQualifiersSurvive:
    """The whole reason Metric carries a note."""

    def test_sma_note_states_what_the_percentage_is(self):
        out = page.render_html(_snap())
        assert "spot -9.1% vs it" in out

    def test_uncovered_window_says_why(self):
        snap = _snap()
        snap["sources"]["price"]["data"] = {
            "spot": 1.0, "source": "x",
            "smas": [{"days": 200, "covered": False, "value": None, "pct": None,
                      "days_available": 60}],
        }
        out = page.render_html(snap)
        assert "n/a" in out and "only 60 days available" in out


class TestEscaping:
    """A snapshot can be ingested, so its free text is untrusted."""

    def test_error_text_is_escaped(self):
        snap = _snap(available=False,
                     error='<script>alert("xss")</script>', data=None)
        out = page.render_html(snap)
        assert "<script>alert" not in out
        assert "&lt;script&gt;" in out

    def test_stale_error_is_escaped(self):
        snap = _snap(stale=True, cached=True, cache_age_seconds=7200,
                     error="<img src=x onerror=alert(1)>")
        out = page.render_html(snap)
        assert "<img src=x" not in out
        assert "&lt;img" in out

    def test_title_is_escaped(self):
        out = page.render_html(_snap(), title="<b>hi</b>")
        assert "<b>hi</b>" not in out and "&lt;b&gt;" in out


class TestDegradation:
    def test_unavailable_source_shows_the_reason(self):
        out = page.render_html(_snap(available=False, error="node unreachable",
                                     data=None))
        assert "unavailable" in out and "node unreachable" in out

    def test_unknown_source_still_renders(self):
        snap = _snap()
        snap["sources"]["quantum"] = {
            "available": True, "stale": False, "cached": False,
            "cache_age_seconds": None, "as_of": None, "error": None,
            "data": {"spooky": 1},
        }
        out = page.render_html(snap)
        assert "QUANTUM" in out and "no renderer in this build" in out

    def test_a_raising_panel_builder_costs_one_card(self, monkeypatch):
        from btc_dashboard.sources import price
        monkeypatch.setattr(price, "html_panels",
                            lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
        out = page.render_html(_snap())
        assert "render failed" in out and "<html" in out


class TestFreshnessBadges:
    def test_live_cached_and_stale_are_distinguished(self):
        assert "live" in page.render_html(_snap())
        assert "cached 15m" in page.render_html(
            _snap(cached=True, cache_age_seconds=900))
        stale = page.render_html(
            _snap(stale=True, cached=True, cache_age_seconds=7200, error="down"))
        assert "STALE 2h" in stale and "refresh failed" in stale


class TestAskBox:
    def test_absent_by_default(self):
        """Static output must not show a control that cannot work."""
        assert "<form" not in page.render_html(_snap())

    def test_present_when_enabled_with_a_cost_warning(self):
        out = page.render_html(_snap(), ask=True)
        assert "<form" in out and 'action="/ask"' in out
        assert "costs money" in out


class TestAvailabilityTicks:
    """The tick is information, so it lives in the markup, not the stylesheet.

    It was originally a CSS `::before` with a hex escape. Two problems, one
    cosmetic and one structural: `\\2713` had to survive two layers of Python
    quoting to reach the browser and a rewrite turned it into an octal escape,
    shipping a superscript one; and a glyph injected by stylesheet disappears
    whenever styling does, taking "which sources are available" with it.
    """

    def _two_sources(self, node_ok: bool):
        snap = _snap()
        snap["sources"]["node"] = {
            "available": node_ok, "stale": False, "cached": False,
            "cache_age_seconds": None, "as_of": None,
            "error": None if node_ok else "bitcoin-cli not found",
            "data": {"height": 1, "hash_rate_ehs": 1.0, "difficulty_t": 1.0,
                     "retarget": {}, "mempool": {}, "fees_sat_vb": {}} if node_ok else None,
        }
        return snap

    def test_glyphs_are_literal_characters_in_the_html(self):
        out = page.render_html(self._two_sources(node_ok=True))
        assert page.TICK_OK in out
        assert "\\2713" not in out and "content:\"\\" not in out

    def test_an_unavailable_source_gets_the_cross(self):
        out = page.render_html(self._two_sources(node_ok=False))
        assert page.TICK_NO in out

    def test_the_marker_survives_the_stylesheet_being_stripped(self):
        """Strip the <style> block; availability must still be readable."""
        import re
        out = page.render_html(self._two_sources(node_ok=False))
        without_css = re.sub(r"<style>.*?</style>", "", out, flags=re.S)
        assert page.TICK_OK in without_css and page.TICK_NO in without_css

    def test_the_css_carries_no_escapes_or_non_ascii(self):
        """Both are what broke it: an escape mangled by quoting, and a glyph
        that only exists in the stylesheet."""
        assert "\\" not in page.CSS
        assert all(ord(c) < 128 for c in page.CSS)

    def test_ticks_name_their_source(self):
        out = page.render_html(self._two_sources(node_ok=True))
        assert "PRICE" in out and "NETWORK" in out
