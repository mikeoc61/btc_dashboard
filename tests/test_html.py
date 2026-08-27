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


class TestFavicon:
    """Two dashboards in tabs need telling apart, and the page must stay
    self-contained while doing it."""

    def test_the_page_declares_an_icon(self):
        out = page.render_html(_snap())
        assert '<link rel="icon"' in out

    def test_it_is_inline_not_a_request(self):
        out = page.render_html(_snap())
        assert 'href="data:image/svg+xml;base64,' in out
        assert 'href="/favicon' not in out

    def test_the_data_uri_round_trips(self):
        import base64
        uri = page._favicon_data_uri()
        decoded = base64.b64decode(uri.split(",", 1)[1]).decode()
        assert decoded == page._FAVICON_SVG
        assert decoded.startswith("<svg") and decoded.endswith("</svg>")

    def test_base64_keeps_the_svg_namespace_out_of_the_page(self):
        """The xmlns is an http:// URL. Percent-encoding the SVG would put it
        in the page as literal text and break the self-contained check."""
        assert "http://" in page._FAVICON_SVG
        assert "http://" not in page.render_html(_snap())

    def test_the_mark_is_drawn_not_typed(self):
        """U+20BF is absent from many fonts; a tab showing a substitution box
        is worse than no icon at all."""
        assert "₿" not in page._FAVICON_SVG
        assert page._FAVICON_SVG.count("<rect") >= 3   # badge plus two strokes

    def test_it_is_small_enough_to_inline(self):
        assert len(page._favicon_data_uri()) < 2000


class TestPngFallbackForSafari:
    """Safari has never read SVG favicons from a data URI — it falls back to
    its own generated letter tile, so the icon appears not to change at all."""

    def test_both_formats_are_offered(self):
        out = page.render_html(_snap())
        assert 'type="image/png"' in out
        assert 'type="image/svg+xml"' in out

    def test_the_png_is_valid(self):
        png = page._favicon_png()
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert png[12:16] == b"IHDR" and png.endswith(b"IEND\xae\x42\x60\x82")

    def test_the_png_declares_the_right_size(self):
        import struct
        png = page._favicon_png()
        w, h = struct.unpack(">II", png[16:24])
        assert (w, h) == (page._ICON_PX, page._ICON_PX) == (32, 32)

    def test_it_is_generated_not_pasted(self):
        """A base64 blob is unreadable in a diff and cannot be checked against
        the SVG it is meant to match."""
        assert page._MARK_RECTS, "the mark is described as geometry"
        assert page._favicon_png() == page._favicon_png(), "and is deterministic"

    def test_both_icons_draw_the_same_mark(self):
        """The SVG and the raster must not drift apart."""
        svg_strokes = page._FAVICON_SVG.count("<rect")
        assert svg_strokes >= 3
        assert len(page._MARK_RECTS) >= 3
        assert "f7931a" in page._FAVICON_SVG.lower()
        assert page._ORANGE == (0xF7, 0x93, 0x1A)

    def test_still_no_external_references(self):
        out = page.render_html(_snap())
        assert "http://" not in out and "https://" not in out

    def test_the_png_stays_small_enough_to_inline(self):
        assert len(page._favicon_png_data_uri()) < 1500


class TestTheReferenceCloseIsDated:
    """Two cards showed two different "previous closes" and neither named its
    day, so an ordinary one-day warehouse lag looked like the sources
    disagreeing about the price."""

    def test_the_price_card_names_the_day(self):
        snap = _snap()
        snap["sources"]["price"]["data"] = {
            "spot": 63011.0, "prev_close": 63031.0,
            "prev_close_date": "2026-08-15", "change_pct": -0.03,
            "source": "coingecko", "smas": [],
        }
        assert "15 Aug close $63,031" in page.render_html(snap)

    def test_the_warehouse_card_names_its_day(self):
        from btc_dashboard.sources import warehouse
        rows = warehouse.html_panels({
            "date": "2026-08-14", "onchain": {}, "signals": {},
            "close": 62979.0,
        })[0].metrics
        close = next(m for m in rows if m.label == "Daily Close")
        assert "2026-08-14" in close.note

    def test_an_undated_close_still_renders(self):
        snap = _snap()
        snap["sources"]["price"]["data"] = {
            "spot": 63011.0, "prev_close": 63031.0, "prev_close_date": None,
            "change_pct": -0.03, "source": "coingecko", "smas": [],
        }
        assert "prev close $63,031" in page.render_html(snap)


class TestTheOnChainDayIsInTheHeading:
    """As a row the date read as one metric among many, so a reader comparing
    the daily close against a live price had no cue they were different days."""

    def _panels(self, date="2026-08-14"):
        from btc_dashboard.sources import warehouse
        return warehouse.html_panels({
            "date": date, "onchain": {"blocks_day": 149}, "signals": {},
            "close": 62979.0,
        })

    def test_the_heading_carries_the_day(self):
        assert "14 AUG" in self._panels()[0].title

    def test_the_weekday_survives(self):
        """fee/subsidy runs materially lower at weekends, so an unlabelled
        Saturday reads as deterioration rather than as a Saturday."""
        assert "FRI" in self._panels()[0].title

    def test_the_date_is_no_longer_a_row(self):
        labels = [m.label for m in self._panels()[0].metrics]
        assert "Date (UTC)" not in labels
        assert "Blocks" in labels

    def test_a_missing_date_falls_back(self):
        assert self._panels(date=None)[0].title == "ON-CHAIN (DAILY)"

    def test_the_sibling_cards_are_not_dated(self):
        """Signals and volatility span history, not that one day."""
        titles = [p.title for p in self._panels()]
        assert not any("14 AUG" in t for t in titles[1:])


class TestCardOrder:
    """Volatility belongs beside price: a distance from a moving average only
    means something in volatility units, and the two come from different
    sources, so source order cannot express it."""

    def _snap(self):
        base = {"available": True, "stale": False, "cached": False,
                "cache_age_seconds": None, "as_of": None, "error": None}
        return {"schema_version": 1, "generated_at": "2026-08-16T02:00:00+00:00",
                "asset": "btc", "sources": {
            "price": {**base, "data": {"spot": 63000.0, "source": "cg", "smas": []}},
            "node": {**base, "data": {"height": 1, "hash_rate_ehs": 900.0,
                                      "difficulty_t": 1.0, "retarget": {},
                                      "mempool": {}, "fees_sat_vb": {}}},
            "warehouse": {**base, "data": {
                "date": "2026-08-15", "onchain": {}, "signals": {},
                "volatility": {"annualisation_days": 365,
                               "percentile_window_days": 730,
                               "windows": [{"days": 30, "covered": True,
                                            "value": 22.5, "percentile_recent": 40.0,
                                            "percentile_all": 20.0}]}}},
        }}

    def _titles(self, snap):
        import re
        return re.findall(r'<h2>([A-Z][^<]{0,40}?)(?:<span|</h2>)',
                          page.render_html(snap))

    def test_volatility_comes_second(self):
        titles = self._titles(self._snap())
        assert titles[0] == "PRICE"
        assert titles[1].startswith("VOLATILITY")

    def test_its_sibling_cards_stay_later(self):
        titles = self._titles(self._snap())
        assert titles.index("NETWORK (LIVE)") < next(
            i for i, t in enumerate(titles) if t.startswith("ON-CHAIN"))

    def test_priority_beats_source_order(self):
        from btc_dashboard.sources import price, warehouse
        assert price.html_panels({"spot": 1.0, "smas": []})[0].priority == 10
        vol = [p for p in warehouse.html_panels(self._snap()["sources"]["warehouse"]["data"])
               if p.title.startswith("VOLATILITY")][0]
        assert vol.priority == 20


class TestNotableStrip:
    """Threshold-selected facts, never interpretation."""

    def _warehouse(self, pct):
        base = {"available": True, "stale": False, "cached": False,
                "cache_age_seconds": None, "as_of": None, "error": None}
        return {"schema_version": 1, "generated_at": "2026-08-16T02:00:00+00:00",
                "asset": "btc", "sources": {"warehouse": {**base, "data": {
            "date": "2026-08-15", "onchain": {}, "signals": {},
            "volatility": {"annualisation_days": 365, "percentile_window_days": 730,
                           "windows": [{"days": 30, "covered": True, "value": 22.5,
                                        "percentile_recent": pct,
                                        "percentile_all": 2.0}]}}}}}

    def test_an_extreme_low_is_surfaced(self):
        out = page.render_html(self._warehouse(0.4))
        assert "NOTABLE" in out and "30d volatility" in out and "pctile of 2y" in out

    def test_an_extreme_high_is_surfaced_too(self):
        """Both tails preceded larger moves; only reporting lows tells half
        the story."""
        assert "NOTABLE" in page.render_html(self._warehouse(98.0))

    def test_an_ordinary_reading_produces_no_strip(self):
        """A strip that always finds something teaches you to ignore it."""
        assert "NOTABLE" not in page.render_html(self._warehouse(45.0))

    def test_it_states_the_window_not_a_conclusion(self):
        out = page.render_html(self._warehouse(0.4))
        assert "pctile of 2y" in out
        for forecast in ("compression", "expect", "breakout", "bullish", "bearish"):
            assert forecast not in out.lower()

    def test_unavailability_leads(self):
        snap = self._warehouse(45.0)
        snap["sources"]["node"] = {"available": False, "stale": False,
                                   "cached": False, "cache_age_seconds": None,
                                   "as_of": None, "error": "no node", "data": None}
        assert "network unavailable" in page.render_html(snap)

    def test_staleness_leads(self):
        snap = self._warehouse(45.0)
        snap["sources"]["warehouse"].update(
            stale=True, cached=True, cache_age_seconds=7200, error="down")
        assert "on-chain is stale (2h old)" in page.render_html(snap)

    def test_a_raising_threshold_check_does_not_cost_the_page(self, monkeypatch):
        from btc_dashboard.sources import warehouse
        monkeypatch.setattr(warehouse, "notable",
                            lambda d: (_ for _ in ()).throw(RuntimeError("boom")))
        assert "<html" in page.render_html(self._warehouse(0.4))


class TestNotableIsInline:
    """A column of bullets pushes the cards the strip introduces below the
    fold, so it lays out as one line."""

    def _snap(self):
        base = {"available": True, "stale": False, "cached": False,
                "cache_age_seconds": None, "as_of": None, "error": None}
        return {"schema_version": 1, "generated_at": "2026-08-16T02:00:00+00:00",
                "asset": "btc", "sources": {"warehouse": {**base, "data": {
            "date": "2026-08-15", "onchain": {}, "signals": {},
            "volatility": {"annualisation_days": 365, "percentile_window_days": 730,
                           "windows": [
                               {"days": 7, "covered": True, "value": 10.3,
                                "percentile_recent": 0.4, "percentile_all": 1.0},
                               {"days": 30, "covered": True, "value": 22.5,
                                "percentile_recent": 0.4, "percentile_all": 2.0}]}}}}}

    def test_no_list_markup(self):
        out = page.render_html(self._snap())
        assert "<li>" not in out and "<ul>" not in out

    def test_items_are_pipe_separated(self):
        assert '<span class="sep">&nbsp;|</span>' in page.render_html(self._snap())

    def test_a_single_item_has_no_trailing_separator(self):
        snap = self._snap()
        snap["sources"]["warehouse"]["data"]["volatility"]["windows"].pop()
        assert '<span class="sep">' not in page.render_html(snap)

    def test_it_still_reads_with_the_stylesheet_stripped(self):
        """Spacing from flex `gap` vanishes with the CSS, running the items
        together as NOTABLE7d volatility 10%|30d volatility 22%."""
        import re
        out = page.render_html(self._snap())
        section = re.search(r'<section class="notable">(.*?)</section>', out, re.S).group(1)
        plain = re.sub(r"<[^>]+>", "", section).replace("&nbsp;", " ")
        assert plain.startswith("NOTABLE 7d volatility")
        assert " | " in plain
        assert "NOTABLE7d" not in plain


class TestUpdatingInPlace:
    """A whole-page refresh replaced the ask box mid-sentence.

    The numbers have to keep moving while someone types a question at them, so
    the data regions are patched from a fragment and the ask box is left alone.
    """

    def _ancestors_of_form_controls(self, markup: str) -> set[str]:
        """Ids of every element containing a form control, however deep."""
        from html.parser import HTMLParser

        VOID = {"input", "br", "img", "meta", "link", "hr"}

        class Walk(HTMLParser):
            def __init__(self):
                super().__init__()
                self.open, self.hits = [], set()

            def handle_starttag(self, tag, attrs):
                ident = dict(attrs).get("id")
                if tag not in VOID:
                    self.open.append(ident)
                if tag in ("form", "input", "button", "textarea"):
                    self.hits.update(i for i in self.open if i)

            def handle_startendtag(self, tag, attrs):
                self.handle_starttag(tag, attrs)

            def handle_endtag(self, tag):
                if tag not in VOID and self.open:
                    self.open.pop()

        w = Walk()
        w.feed(markup)
        return w.hits

    def test_the_ask_box_is_in_no_region_a_tick_overwrites(self):
        """The regression itself: if the box lives inside a patched region, an
        update replaces the field and whatever was typed into it."""
        out = page.render_html(_snap(), ask=True, live_endpoint="/live")
        clobbered = self._ancestors_of_form_controls(out) & set(page.LIVE_IDS)
        assert not clobbered, f"a tick would replace the ask box inside {clobbered}"

    def test_the_fragment_carries_no_controls_at_all(self):
        """Belt and braces: nothing the updater writes can be a control."""
        assert "<form" not in page.render_live(_snap())
        assert "<input" not in page.render_live(_snap())

    def test_the_answer_survives_a_tick(self):
        answer = {"question": "why?", "text": "because", "provider": "p",
                  "model": "m", "input_tokens": 1, "output_tokens": 2}
        out = page.render_html(_snap(), ask=True, answer=answer,
                               live_endpoint="/live")
        assert "because" in out
        assert "because" not in page.render_live(_snap())

    def test_page_and_fragment_cannot_drift(self):
        """Both are built from `_live_parts`, so the updater always patches
        markup shaped like the page it is patching."""
        snap = _snap()
        page_out, fragment = page.render_html(snap), page.render_live(snap)
        for part in page._live_parts(snap).values():
            assert part in page_out
            assert part in fragment

    def test_every_named_region_exists_in_both(self):
        snap = _snap()
        for ident in page.LIVE_IDS:
            marker = f'id="{ident}"'
            assert marker in page.render_html(snap), f"{ident} missing from the page"
            assert marker in page.render_live(snap), f"{ident} missing from the fragment"

    def test_an_empty_notable_strip_keeps_its_slot(self):
        """The strip is absent when nothing qualifies. Its wrapper is not, or
        the update has nowhere to put one back."""
        out = page.render_html(_snap())
        assert '<section class="notable">' not in out
        assert 'id="notable"' in out


class TestRefreshMechanism:
    def test_a_live_endpoint_replaces_the_document_reload(self):
        out = page.render_html(_snap(), live_endpoint="/live")
        assert "<noscript><meta http-equiv=\"refresh\"" in out, \
            "reloading stays as the fallback for a browser without scripts"
        assert "/live" in out and "setInterval" in out

    def test_without_one_the_document_still_reloads(self):
        """A file:// page and a static server have nothing to fetch from."""
        out = page.render_html(_snap())
        assert "http-equiv=\"refresh\"" in out and "<noscript>" not in out
        assert "setInterval" not in out

    def test_refresh_none_updates_by_neither_route(self):
        out = page.render_html(_snap(), refresh=None, live_endpoint="/live")
        assert "http-equiv=\"refresh\"" not in out and "setInterval" not in out

    def test_the_interval_is_the_refresh_seconds(self):
        assert "setInterval(tick, 30000)" in page.render_html(
            _snap(), refresh=30, live_endpoint="/live")

    def test_the_updater_keeps_the_page_self_contained(self):
        out = page.render_html(_snap(), ask=True, live_endpoint="/live")
        for external in ("<script src", "http://", "https://"):
            assert external not in out, f"page must not reference {external}"

    def test_the_endpoint_cannot_open_a_tag_from_inside_the_script(self):
        out = page.render_html(_snap(), live_endpoint="/live</script><b>")
        assert "</script><b>" not in out.split("<script>")[1]
        assert "\\u003c" in out


class TestQueriesAreShown:
    """A figure that came from a query nobody can see is not checkable, and
    checkability is the reason the analyst reads a local warehouse at all."""

    def _answer(self, calls):
        return {"question": "how does this drawdown compare?", "text": "an answer",
                "provider": "p", "model": "m", "input_tokens": 1, "output_tokens": 2,
                "tool_calls": calls}

    def _call(self, sql="SELECT date, close FROM btc", result="date | close\n2026-08-25 | 63000"):
        from btc_dashboard.providers import ToolCall
        return ToolCall("query_warehouse", {"sql": sql}, result)

    def test_the_sql_appears_on_the_page(self):
        out = page.render_html(_snap(), ask=True, answer=self._answer([self._call()]))
        assert "SELECT date, close FROM btc" in out

    def test_the_rows_appear_too(self):
        out = page.render_html(_snap(), ask=True, answer=self._answer([self._call()]))
        assert "2026-08-25 | 63000" in out

    def test_the_count_is_stated(self):
        out = page.render_html(_snap(), ask=True,
                               answer=self._answer([self._call(), self._call()]))
        assert "2 queries run" in out
        one = page.render_html(_snap(), ask=True, answer=self._answer([self._call()]))
        assert "1 query run" in one

    def test_an_answer_with_no_queries_shows_no_disclosure(self):
        out = page.render_html(_snap(), ask=True, answer=self._answer([]))
        assert "queries run" not in out and "query run" not in out

    def test_the_sql_is_escaped(self):
        """Written by the model, so it is not trusted markup."""
        out = page.render_html(_snap(), ask=True, answer=self._answer(
            [self._call(sql="SELECT '<script>alert(1)</script>'")]))
        assert "<script>alert(1)" not in out and "&lt;script&gt;" in out

    def test_the_rows_are_escaped(self):
        """Rows come out of a database filled from remote APIs."""
        out = page.render_html(_snap(), ask=True, answer=self._answer(
            [self._call(result="<img src=x onerror=alert(1)>")]))
        assert "<img src=x" not in out and "&lt;img" in out

    def test_a_long_result_is_trimmed_and_says_so(self):
        rows = "\n".join(f"2026-01-{i:02d} | {i}" for i in range(1, 40))
        out = page.render_html(_snap(), ask=True,
                               answer=self._answer([self._call(result=rows)]))
        assert "more lines" in out
        assert "2026-01-39" not in out

    def test_it_survives_the_stylesheet_being_stripped(self):
        """`details` is markup the browser understands, not styling. Strip the
        CSS and the query is still in the document."""
        import re
        out = page.render_html(_snap(), ask=True, answer=self._answer([self._call()]))
        stripped = re.sub(r"<style>.*?</style>", "", out, flags=re.S)
        assert "SELECT date, close FROM btc" in stripped


class TestTheAnswerSaysWhenItCouldNotCheck:
    def _answer(self, **over):
        base = {"question": "how does this compare to 2022?", "text": "an answer",
                "provider": "p", "model": "m", "input_tokens": 1, "output_tokens": 2,
                "tool_calls": ()}
        base.update(over)
        return base

    def test_the_reason_appears_on_the_card(self):
        out = page.render_html(_snap(), ask=True, answer=self._answer(
            no_tools_reason="No warehouse query tool was available, so this was "
                            "answered from the snapshot alone."))
        assert "answered from the snapshot alone" in out

    def test_nothing_is_added_when_a_tool_was_available(self):
        out = page.render_html(_snap(), ask=True, answer=self._answer())
        assert "snapshot alone" not in out

    def test_it_is_escaped(self):
        out = page.render_html(_snap(), ask=True, answer=self._answer(
            no_tools_reason="<img src=x onerror=alert(1)>"))
        assert "<img src=x" not in out and "&lt;img" in out

    def test_it_survives_the_stylesheet_being_stripped(self):
        """`warn` is a colour. The sentence has to carry the meaning itself."""
        import re
        out = page.render_html(_snap(), ask=True,
                               answer=self._answer(no_tools_reason="no tool was available"))
        stripped = re.sub(r"<style>.*?</style>", "", out, flags=re.S)
        assert "no tool was available" in stripped
