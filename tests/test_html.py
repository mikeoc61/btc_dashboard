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


class TestSpotChange:
    """Spot is coloured against the previous completed daily close.

    The reference comes from the price source's own series, not the
    warehouse's: those are different venues, and mixing them would put a venue
    spread into a figure meant to show the day's move.
    """

    def _with(self, spot, prev):
        snap = _snap()
        snap["sources"]["price"]["data"] = {
            "spot": spot, "prev_close": prev, "source": "coingecko",
            "change_pct": round((spot - prev) / prev * 100, 2) if prev else None,
            "smas": [],
        }
        return snap

    def test_a_rise_is_green(self):
        from btc_dashboard.sources import price
        assert price.change_tone(1.5) == "up"
        assert 'class="value up"' in page.render_html(self._with(64000, 63000))

    def test_a_fall_is_red(self):
        from btc_dashboard.sources import price
        assert price.change_tone(-1.5) == "down"
        assert 'class="value down"' in page.render_html(self._with(62000, 63000))

    def test_a_negligible_move_stays_neutral(self):
        """Green for +0.03% asserts a direction the number does not carry."""
        from btc_dashboard.sources import price
        assert price.change_tone(0.03) is None
        assert price.change_tone(-0.03) is None
        out = page.render_html(self._with(63019, 63000))
        assert 'class="value up"' not in out and 'class="value down"' not in out

    def test_the_reference_close_is_shown(self):
        out = page.render_html(self._with(64000, 63000))
        assert "+1.59% vs prev close $63,000" in out

    def test_no_previous_close_means_no_colour(self):
        from btc_dashboard.sources import price
        assert price.change_tone(None) is None
        out = page.render_html(self._with(63000, None))
        assert "$63,000" in out

    def test_the_analyst_is_told_it_is_not_a_24h_change(self):
        from btc_dashboard.sources import price
        ctx = " ".join(price.context_lines(self._with(64000, 63000)
                                           ["sources"]["price"]["data"]))
        assert "not a 24-hour change" in ctx
        assert "$63,000" in ctx


class TestToneLandsOnTheSignedThing:
    """A red $63,955 reads as "the average fell". What is negative is spot's
    distance from it, and that lives in the note."""

    def test_sma_level_is_not_coloured(self):
        from btc_dashboard.sources import price
        rows = price.html_panels({
            "spot": 63014.0, "source": "cg",
            "smas": [{"days": 20, "covered": True, "value": 63955.0,
                      "pct": -1.5, "position": "near"}],
        })[0].metrics
        sma = next(m for m in rows if m.label == "20D SMA")
        assert sma.tone is None, "the level itself is not signed"
        assert sma.note_tone == "down", "the distance is"

    def test_hashrate_level_is_not_coloured(self):
        from btc_dashboard.sources import node
        rows = node.html_panels({
            "height": 1, "hash_rate_ehs": 900.0, "hash_rate_7d_pct": -0.62,
            "difficulty_t": 1.0, "retarget": {}, "mempool": {}, "fees_sat_vb": {},
        })[0].metrics
        hr = next(m for m in rows if m.label == "Hashrate")
        assert hr.tone is None and hr.note_tone == "down"

    def test_a_signed_value_still_colours_the_value(self):
        """Where the value *is* the signed quantity, it keeps the colour."""
        from btc_dashboard.sources import node
        rows = node.html_panels({
            "height": 1, "hash_rate_ehs": 1.0, "difficulty_t": 1.0,
            "retarget": {"projection_pct": -2.09, "blocks_left": 100},
            "mempool": {}, "fees_sat_vb": {},
        })[0].metrics
        rt = next(m for m in rows if m.label == "Next Retarget")
        assert rt.tone == "down"

    def test_note_tone_reaches_the_markup(self):
        snap = _snap()
        snap["sources"]["price"]["data"] = {
            "spot": 63014.0, "source": "cg",
            "smas": [{"days": 20, "covered": True, "value": 63955.0,
                      "pct": -1.5, "position": "near"}],
        }
        out = page.render_html(snap)
        assert 'class="note down"' in out
        assert 'class="value down"' not in out


class TestEveryCardOfASourceIsDated:
    """One source can produce several cards and the grid wraps them onto
    different rows, so a badge on the first alone leaves the rest undated."""

    def _warehouse_snap(self, **over):
        block = {
            "available": True, "stale": False, "cached": True,
            "cache_age_seconds": 900, "as_of": "2026-08-14", "error": None,
            "data": {
                "date": "2026-08-14", "onchain": {"blocks_day": 149},
                "signals": {"fee_pctile": 34.0, "apathy_days": 2},
                "volatility": {"annualisation_days": 365,
                               "percentile_window_days": 730,
                               "windows": [{"days": 30, "covered": True,
                                            "value": 22.5,
                                            "percentile_recent": 0.4,
                                            "percentile_all": 2.0}]},
            },
        }
        block.update(over)
        return {"schema_version": 1, "generated_at": "2026-08-16T00:07:20+00:00",
                "asset": "btc", "sources": {"warehouse": block}}

    def test_all_three_warehouse_cards_carry_the_badge(self):
        out = page.render_html(self._warehouse_snap())
        assert out.count("ON-CHAIN") >= 1 and "SIGNALS" in out and "VOLATILITY" in out
        assert out.count("cached 15m") == 3, "each card must state its vintage"

    def test_stale_is_announced_on_every_card(self):
        out = page.render_html(self._warehouse_snap(
            stale=True, error="warehouse unreadable"))
        assert out.count("STALE 15m") == 3

    def test_the_failure_reason_appears_once(self):
        """Repeating one error three times reads as three problems."""
        out = page.render_html(self._warehouse_snap(
            stale=True, error="warehouse unreadable"))
        assert out.count("warehouse unreadable") == 1
