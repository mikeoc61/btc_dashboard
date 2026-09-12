"""The balance-of-evidence card: a digest that must not become a score.

Two properties carry most of the weight here. A row that cannot be filled has
to stay on the card as `n/a` — the moment a missing reading can vanish, six
readings quietly become five and the card renormalises onto whatever reported.
And no row may lose the window it was measured over, since that is the only
thing making it comparable to a figure from anywhere else.
"""
from __future__ import annotations

import pytest

from btc_dashboard import composite, html as page, render
from btc_dashboard.sources import flows, node, price, warehouse

# Words the page is not allowed to say, kept in step with the notable strip's
# own guard. A digest is where a forecast would slip in most naturally: six
# readings side by side look like they are asking to be totalled.
FORECASTS = ("compression", "expect", "breakout", "bullish", "bearish")

PRICE = {
    "spot": 63939.0, "source": "coingecko", "prev_close": 62810.0,
    "prev_close_date": "2026-09-10", "change_pct": 1.8,
    "smas": [{"days": 200, "covered": True, "value": 71840.0, "pct": -11.0,
              "position": "below", "days_available": 201}],
    "sma200": 71840.0, "sma200_pct": -11.0, "sma200_position": "below",
    "rsi_live": {"period": 14, "bars_available": 201, "covered": True, "value": 46.3},
    "rsi_close": {"period": 14, "bars_available": 200, "covered": True, "value": 44.1},
    "days_available": 201,
}
NODE = {
    "height": 960168, "hash_rate_ehs": 876.61, "hash_rate_7d_pct": -0.22,
    "difficulty_t": 126.23, "retarget": {}, "mempool": {}, "fees_sat_vb": {},
}
WAREHOUSE = {
    # `close_date` is the `btc` table's own day and is deliberately not `date`,
    # the on-chain frontier — the two advance independently, and the balance
    # rows are dated by the close.
    "date": "2026-09-09", "close_date": "2026-09-10", "onchain": {},
    "signals": {"trades_pctile": 88.0},
    "volatility": {"annualisation_days": 365, "windows": [
        {"days": 30, "covered": True, "value": 24.8, "percentile_recent": 17.0,
         "percentile_all": 8.0, "percentile_window_days": 730,
         "days_available": 730}]},
}
FLOWS = {
    # Farside's own format, which is what `dated()` parses.
    "as_of": "10 Sep 2026", "age_days": 1, "lead": "IBIT",
    "windows": [{"days": 5, "covered": True, "total": -457.4, "lead": -300.1,
                 "days_available": 5}],
    "streak_days": 3, "streak_sign": "outflow",
}
PAYLOADS = {"price": PRICE, "node": NODE, "warehouse": WAREHOUSE, "flows": FLOWS}
MODULES = {"price": price, "node": node, "warehouse": warehouse, "flows": flows}


def _block(data, **over):
    block = {"available": True, "stale": False, "cached": False,
             "cache_age_seconds": None, "cache_ttl_seconds": None,
             "as_of": None, "error": None, "data": data}
    block.update(over)
    return block


def _band(out: str) -> str:
    """Just the balance band, from its wrapper to the start of the card grid.

    Sliced at the grid rather than at `</main>`, which would swallow the cards
    and the ask box and make every containment assertion here vacuous.
    """
    return out.split('id="composite"', 1)[1].split('<div class="grid"', 1)[0]


def _snap(down: tuple[str, ...] = ()) -> dict:
    sources = {}
    for name, data in PAYLOADS.items():
        sources[name] = (
            _block(None, available=False, error=f"{name} is not here")
            if name in down else _block(dict(data))
        )
    return {"schema_version": 1, "generated_at": "2026-09-11T09:14:00+00:00",
            "asset": "btc", "sources": sources}


class TestTheEmptyDictContract:
    """Every source's `balance_rows` must survive `{}` with its labels intact.

    This is what lets a source that is *down* keep its rows on the card. Get it
    wrong and the failure is silent and directional: the card shrinks to the
    sources that happen to be up, which is renormalisation by another name.
    """

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    def test_the_labels_are_the_same_either_way(self, name):
        mod = MODULES[name]
        full = [m.label for m in mod.balance_rows(PAYLOADS[name])]
        empty = [m.label for m in mod.balance_rows({})]
        assert full == empty and full, name

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    def test_an_empty_payload_reports_no_values(self, name):
        assert all(m.value == composite.NA
                   for m in MODULES[name].balance_rows({}))

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    def test_a_full_payload_reports_every_one(self, name):
        assert all(m.value != composite.NA
                   for m in MODULES[name].balance_rows(PAYLOADS[name]))


class TestAMissingReadingKeepsItsRow:
    def test_a_dead_source_still_occupies_the_card(self):
        """The count of rows is a property of the card, not of what is up."""
        assert len(composite.rows(_snap())) == len(
            composite.rows(_snap(down=("node", "warehouse"))))

    def test_the_dead_rows_read_n_a(self):
        rows = {m.label: m.value
                for m in composite.rows(_snap(down=("warehouse",)))}
        assert rows["Speculation"] == "n/a" and rows["Participation"] == "n/a"
        assert rows["Trend"] != "n/a"

    def test_the_coverage_count_says_how_many_are_missing(self):
        rows = composite.rows(_snap(down=("node",)))
        assert composite.coverage(rows) == (5, 6)
        assert composite.coverage_label(rows) == "5 of 6 readings"
        assert not composite.complete(rows)

    def test_a_whole_card_reports_itself_whole(self):
        assert composite.complete(composite.rows(_snap()))

    def test_the_reason_is_stated_once_per_source(self):
        """One warehouse that is missing owns two rows. The same sentence under
        both reads as two problems, which is why the cards state a failure on
        the source's first card only."""
        notes = [m.note for m in composite.rows(_snap(down=("warehouse",)))
                 if m.value == "n/a"]
        assert notes.count("warehouse is not here") == 1
        assert notes[1] is None

    def test_quiet_mode_drops_the_reason_and_keeps_the_row(self):
        """`--quiet` suppresses why a source is down. It does not suppress the
        fact that a reading is missing — that would leave a thinner card
        looking like a complete one."""
        rows = composite.rows(_snap(down=("node",)), reasons=False)
        assert composite.coverage(rows) == (5, 6)
        assert "is not here" not in " ".join(str(m.note) for m in rows)


class TestEveryReadingCarriesItsWindow:
    def test_no_reported_row_is_bare(self):
        """A row without its window is a number that cannot be compared to
        anyone else's — the regression this project keeps having."""
        for m in composite.rows(_snap()):
            if m.value != "n/a":
                assert m.note, m.label

    @pytest.mark.parametrize("fragment", [
        "200d SMA",          # trend names the average it is measured from
        "Wilder",            # the RSI variant, not just "RSI"
        "1008 blocks",       # the hashrate estimate's own window
        "pctile of 2y",      # the rank's window
        "weekday-adjusted",  # and the seasonal correction behind it
        "Farside Total basis",
    ])
    def test_the_qualifiers_are_on_the_card(self, fragment):
        assert fragment in " ".join(str(m.note) for m in composite.rows(_snap()))

    def test_every_reading_that_is_not_live_names_its_day(self):
        """Three of the six are not live: the two warehouse rows, which are
        structurally a day behind, and the flow window, which ends on the last
        fully-reported day. They sit between rows taken this second, under a
        page stamp of today, so an undated one inherits today by proximity —
        and reads as participation that is happening now."""
        notes = {m.label: str(m.note) for m in composite.rows(_snap())}
        assert "through 10 Sep" in notes["Speculation"]
        assert "through 10 Sep" in notes["Participation"]
        assert "through Thu 10 Sep 2026" in notes["Liquidity"]

    def test_the_live_readings_are_not_dated(self):
        """The page stamp already dates them, and a date on a live figure
        invites reading the two undated ones as live too."""
        notes = {m.label: str(m.note) for m in composite.rows(_snap())}
        for label in ("Trend", "Momentum", "Network"):
            assert "through" not in notes[label]

    def test_an_undatable_payload_drops_the_phrase_rather_than_faking_one(self):
        snap = _snap()
        snap["sources"]["warehouse"]["data"] = dict(WAREHOUSE, close_date=None)
        notes = {m.label: str(m.note) for m in composite.rows(snap)}
        assert "through" not in notes["Speculation"]
        assert "pctile of 2y" in notes["Speculation"]

    def test_the_annualisation_travels_with_the_volatility(self):
        """17% of a reading, and enough to move it across a published
        threshold. A digest is exactly where it gets dropped for tidiness."""
        notes = " ".join(str(m.note) for m in composite.rows(_snap()))
        assert "√365" in notes


class TestItIsNotAScore:
    def test_nothing_is_totalled(self):
        out = page.render_html(_snap())
        assert "/100" not in out
        for word in FORECASTS:
            assert word not in out.lower()

    def test_the_card_says_it_is_not_weighted(self):
        """The note is the only thing standing between this card and the
        weighted index it will be asked to become."""
        assert "weighted" in composite.NOTE and "scored" in composite.NOTE
        assert composite.NOTE in page.render_html(_snap()).replace("&#x27;", "'")

    def test_the_undirected_readings_carry_no_tone(self):
        """Volatility fires at both tails and trade count is participation.
        A colour on either asserts a direction the measure does not have, and
        on this card a green figure reads as a vote."""
        tones = {m.label: (m.tone, m.note_tone) for m in composite.rows(_snap())}
        assert tones["Speculation"] == (None, None)
        assert tones["Participation"] == (None, None)
        assert tones["Momentum"] == (None, None)
        assert tones["Trend"][0] and tones["Liquidity"][0] and tones["Network"][0]


class TestNoValueIsColouredWithoutASign:
    def test_an_unmeasured_trend_is_not_painted(self):
        """An ingested payload can say `covered` with a null distance. A bare
        sign test sends that to the `else` branch and prints `n/a` in red,
        which reads as a trend that fell rather than as one nobody measured —
        the bug the retarget projection already had."""
        snap = _snap()
        snap["sources"]["price"]["data"] = dict(
            PRICE, smas=[{"days": 200, "covered": True, "value": 71840.0,
                          "pct": None, "position": "below",
                          "days_available": 201}], sma200_pct=None)
        trend = next(m for m in composite.rows(snap) if m.label == "Trend")
        assert trend.value == "n/a" and trend.tone is None

    @pytest.mark.parametrize("name", sorted(PAYLOADS))
    def test_no_empty_payload_colours_anything(self, name):
        assert all(m.tone is None and m.note_tone is None
                   for m in MODULES[name].balance_rows({}))


class TestItNeverCostsThePage:
    def test_a_raising_source_loses_its_rows_and_nothing_else(self, monkeypatch):
        monkeypatch.setattr(price, "balance_rows",
                            lambda d: (_ for _ in ()).throw(ValueError("boom")))
        rows = composite.rows(_snap())
        assert [m.label for m in rows] == [
            "Network", "Speculation", "Participation", "Liquidity"]
        assert "BALANCE OF EVIDENCE" in page.render_html(_snap())

    def test_a_snapshot_with_no_known_source_renders_no_card(self):
        snap = {"schema_version": 1, "generated_at": "2026-09-11T09:14:00+00:00",
                "asset": "btc", "sources": {"martian": _block({"x": 1})}}
        assert composite.rows(snap) == []
        assert "BALANCE OF EVIDENCE" not in page.render_html(snap)


class TestMeaningSurvivesThePresentation:
    def test_the_terminal_block_reads_the_same_without_colour(self):
        import re
        painted = render.render(_snap(), color=True)
        assert "\x1b" in painted
        assert re.sub(r"\x1b\[[0-9;]*m", "", painted) == render.render(
            _snap(), color=False)

    def test_the_card_reads_the_same_without_the_stylesheet(self):
        import re
        out = page.render_html(_snap())
        stripped = re.sub(r"<style>.*?</style>", "", out, flags=re.S)
        for fragment in ("BALANCE OF EVIDENCE", "6 of 6 readings",
                         "Participation", "participation, not direction"):
            assert fragment in stripped

    def test_no_terminal_line_starts_a_second_one(self):
        """`render()` indents only the first physical line of a body string, so
        a newline anywhere in here starts a line at column 0 — which is the
        shape of a block heading."""
        assert all("\n" not in ln
                   for ln in composite.lines(composite.rows(_snap())))


class TestWhereItLives:
    def test_the_band_is_inside_the_captured_region(self):
        """A PNG is the copy most likely to be read away from the page, so the
        digest has to be in it. Both regions are named, not a wrapper around
        them: the capture clones each into one flat stage, which reproduces a
        stack exactly."""
        assert "composite" in page.CAPTURE_IDS and "notable" in page.CAPTURE_IDS
        assert "lead" not in page.CAPTURE_IDS
        assert "BALANCE OF EVIDENCE" in _band(page.render_html(_snap()))

    def test_the_readings_are_cells_so_the_band_can_lay_them_out(self):
        """Three across, not six stacked. The markup has to carry that — a
        stacked list in a grid container puts every row in one column."""
        band = _band(page.render_html(_snap()))
        assert band.count('class="bcell"') == 6
        assert 'class="bgrid"' in band

    def test_a_cell_keeps_its_tone_and_its_note(self):
        """The cells are built by handing `_rows` one metric at a time, so a
        second copy of the tone and escaping rules cannot drift from the
        first."""
        band = _band(page.render_html(_snap()))
        assert 'class="value down"' in band          # Trend, negative
        assert "spot vs 200d SMA" in band            # and its qualifier

    def test_a_tick_updates_it(self):
        assert "composite" in page.LIVE_IDS
        assert "BALANCE OF EVIDENCE" in page.render_live(_snap())

    def test_the_ask_box_is_outside_it(self):
        """The band is a live region — a tick replaces it wholesale. The box
        must not be anywhere inside one."""
        assert "askform" not in _band(page.render_html(_snap(), ask=True))

    def test_an_ordinary_day_leaves_the_strip_empty(self):
        """Nothing to lead with renders an empty wrapper, not an empty box.
        The band below it is unaffected — it is a separate region now, which is
        the whole reason the strip can come and go without moving anything."""
        quiet = _snap()
        quiet["sources"]["warehouse"]["data"]["signals"] = {"trades_pctile": 48.0}
        quiet["sources"]["flows"]["data"]["streak_days"] = 2
        for w in quiet["sources"]["warehouse"]["data"]["volatility"]["windows"]:
            w["percentile_recent"] = 47.0
        out = page.render_html(quiet)
        assert '<div id="notable"></div>' in out
        assert "BALANCE OF EVIDENCE" in out

    def test_nothing_but_the_strips_label_claims_the_lead_class(self):
        """`.lead` is a short, generic name already bound to the one-word label
        inside the strip, styled inline with a right margin. A second rule on
        it — a "lead section", say — turns that span into whatever the new rule
        says, and a display rule turns it into a block: the label breaks onto
        its own line and reads as a heading over the items rather than as the
        start of them. Cost a rework once already."""
        import re

        # Selectors only: the comments talk about `.lead` on purpose.
        stripped = re.sub(r"/\*.*?\*/", "", page.CSS, flags=re.S)
        selectors = [chunk.split("{", 1)[0].strip()
                     for chunk in stripped.split("}") if "{" in chunk]
        assert [sel for sel in selectors if ".lead" in sel] == [".notable .lead"]

    def test_the_digest_stays_out_of_the_analyst_prompt(self):
        """Every reading on it is already in that context, phrased by the
        source it came from. Repeating six of them would imply an emphasis the
        data has not earned, and a model shown a digest reasons over it."""
        from btc_dashboard import analyst

        assert composite.TITLE not in analyst.build_context(_snap())
