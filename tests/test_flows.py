"""Flow-parsing and summary semantics — the properties that are easy to break."""
from __future__ import annotations

import pytest

from btc_dashboard.sources import flows


class TestParseFlow:
    def test_reported_zero_is_not_missing(self):
        assert flows.parse_flow("0.0") == 0.0
        assert flows.parse_flow("0") == 0.0

    @pytest.mark.parametrize("cell", ["", "-", "   ", "n/a"])
    def test_unreported_is_none(self, cell):
        assert flows.parse_flow(cell) is None

    def test_accounting_negatives_and_separators(self):
        assert flows.parse_flow("(444.5)") == -444.5
        assert flows.parse_flow("1,234.5") == 1234.5
        assert flows.parse_flow("–12.3") == -12.3  # en-dash as minus


def _day(date, ibit, fbtc, arkb, gbtc, total):
    return {
        "date": date, "IBIT": ibit, "FBTC": fbtc,
        "ARKB": arkb, "GBTC": gbtc, "Total": total,
    }


def _complete_days(n, per_day=-10.0, start=1):
    return [
        _day(f"{i} Jan 2026", per_day, 0.0, 0.0, 0.0, per_day)
        for i in range(start, start + n)
    ]


class TestSummarize:
    def test_uncovered_window_is_none_not_a_shorter_sum(self):
        s = flows.summarize(_complete_days(10))
        w5, w20, w60 = s["windows"]
        assert w5["covered"] and w5["total"] == -50.0
        for w in (w20, w60):
            assert w["covered"] is False
            assert w["total"] is None and w["lead"] is None
            assert w["days_available"] == 10

    def test_partial_day_excluded_from_metrics(self):
        rows = _complete_days(5)
        # Newest day: IBIT and FBTC in, ARKB/GBTC still pending.
        rows.append(_day("6 Jan 2026", 100.0, 5.0, None, None, 120.0))
        s = flows.summarize(rows)

        assert s["as_of"] == "5 Jan 2026", "as_of must be the last FULLY reported day"
        assert s["latest_total"] == -10.0
        assert s["windows"][0]["total"] == -50.0, "partial day must not enter the window"

        p = s["partial"]
        assert p["date"] == "6 Jan 2026"
        assert p["reported_total"] == 105.0
        assert p["pending"] == ["ARKB", "GBTC"]
        # Total - tracked = untracked funds, NOT the pending funds' value.
        assert p["other"] == 15.0

    def test_streak_counts_same_sign_complete_days(self):
        rows = _complete_days(3)
        rows.append(_day("9 Jan 2026", 5.0, 0.0, 0.0, 0.0, 5.0))
        s = flows.summarize(rows)
        assert (s["streak_days"], s["streak_sign"]) == (1, "inflow")

    def test_no_complete_day_yields_empty_summary(self):
        rows = [_day("1 Jan 2026", 10.0, None, None, None, 12.0)]
        s = flows.summarize(rows)
        assert s["as_of"] is None
        assert s["latest_total"] is None
        assert s["days_complete"] == 0
        assert all(w["covered"] is False for w in s["windows"])


class TestClassify:
    """The tag carries direction.

    "conviction" alone reads as conviction *buying* in English, so an outflow
    window tagged with the bare word said the opposite of what the data meant.
    """

    def test_lead_dominant_outflow_is_conviction_distribution(self):
        assert flows.classify(-100.0, -80.0) == "conviction distribution"

    def test_lead_dominant_inflow_is_conviction_accumulation(self):
        assert flows.classify(100.0, 80.0) == "conviction accumulation"

    def test_lead_exceeding_total_is_offsetting(self):
        assert flows.classify(-100.0, -150.0) == "offsetting distribution"
        assert flows.classify(100.0, 150.0) == "offsetting accumulation"

    def test_lead_minority_is_broad(self):
        assert flows.classify(-100.0, -20.0) == "broad distribution"
        assert flows.classify(100.0, 20.0) == "broad accumulation"

    def test_lead_against_total_names_the_disagreement(self):
        assert flows.classify(-100.0, 40.0) == "distribution against IBIT"
        assert flows.classify(100.0, -40.0) == "accumulation against IBIT"

    def test_no_total_is_unclassified(self):
        assert flows.classify(None, -10.0) is None
        assert flows.classify(0.0, 0.0) is None

    def test_share_is_reported(self):
        assert flows.lead_share(-494.4, -388.5) == pytest.approx(0.786, abs=0.001)
        assert flows.lead_share(0.0, 1.0) is None
        assert flows.lead_share(1.0, None) is None


class TestRegimeIsAttachedToItsOwnWindow:
    """Regression: the tag was rendered on the streak line.

    On 29 Jul the 5d window was a -494.4M net OUTFLOW with IBIT at 79% of it,
    while the streak was a 1-day INFLOW. Printing "streak 1d inflow —
    conviction" read as conviction buying: the tag described a measure with
    the opposite sign to the one it sat beside.
    """

    def _data(self):
        return {
            "lead": "IBIT", "as_of": "29 Jul 2026", "age_days": 1,
            "latest_total": 32.1, "latest_lead": 89.8, "days_complete": 639,
            "windows": [
                {"days": 5, "days_available": 5, "covered": True,
                 "total": -494.4, "lead": -388.5},
                {"days": 20, "days_available": 20, "covered": True,
                 "total": 205.1, "lead": 166.9},
            ],
            "streak_days": 1, "streak_sign": "inflow",
            "regime": "conviction distribution", "regime_window_days": 5,
            "lead_share_pct": 78.6, "partial": None,
        }

    def test_streak_line_carries_no_regime_tag(self):
        streak = next(l for l in flows.render_lines(self._data())
                      if l.startswith("streak"))
        assert streak == "streak 1d inflow"
        assert "conviction" not in streak

    def test_tag_sits_on_the_window_it_describes(self):
        line = next(l for l in flows.render_lines(self._data())
                    if l.startswith("5d net"))
        assert "-494.4M" in line
        assert "79% IBIT" in line
        assert "conviction distribution" in line

    def test_other_windows_are_untagged(self):
        line = next(l for l in flows.render_lines(self._data())
                    if l.startswith("20d net"))
        assert "conviction" not in line

    def test_analyst_is_told_the_two_can_disagree(self):
        ctx = " ".join(flows.context_lines(self._data()))
        assert "describes the 5d window ONLY" in ctx
        assert "not a property of the streak" in ctx
        assert "opposite directions" in ctx

    def test_summary_records_which_window_the_tag_came_from(self):
        rows = _complete_days(5, per_day=-10.0)
        s = flows.summarize(rows)
        assert s["regime_window_days"] == 5
        assert s["lead_share_pct"] == pytest.approx(100.0)
        assert s["regime"] == "conviction distribution"


class TestParseTable:
    HTML = """
    <table>
      <tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>ARKB</th><th>GBTC</th>
          <th>OTHER</th><th>Total</th></tr>
      <tr><td>2 Jan 2026</td><td>10.0</td><td>0.0</td><td>-</td><td>(5.0)</td>
          <td>1.0</td><td>6.0</td></tr>
    </table>
    """

    def test_columns_resolved_by_name_not_position(self):
        rows = flows.parse_table(self.HTML)
        assert len(rows) == 1
        r = rows[0]
        # An untracked OTHER column sits between GBTC and Total; index-based
        # parsing would read it as the total.
        assert r["Total"] == 6.0
        assert r["GBTC"] == -5.0
        assert r["FBTC"] == 0.0
        assert r["ARKB"] is None

    def test_missing_table_raises(self):
        with pytest.raises(ValueError, match="flow table not found"):
            flows.parse_table("<html><body><p>nothing here</p></body></html>")


class TestTheTbodyWrapperDoesNotDuplicateARow:
    """Farside opens the body with a stray `<tr>` that never closes, so
    html.parser nests every real row inside it. The wrapper has no cells of its
    own, but `find_all` descends and its leading cells alias the first data
    row — which emitted the launch day twice, inflating `days_complete` by one.

    Harmless for the windows only because the phantom lands oldest and
    `complete[-days:]` takes the newest. That is a property of the site's
    markup, not a guarantee.
    """

    HTML = """
    <table>
      <tr><th>Date</th><th>IBIT</th><th>FBTC</th><th>ARKB</th>
          <th>GBTC</th><th>Total</th></tr>
      <tr>
        <tr><td>11 Jan 2024</td><td>111.7</td><td>227.0</td><td>65.3</td>
            <td>(95.1)</td><td>655.3</td></tr>
        <tr><td>12 Jan 2024</td><td>10.0</td><td>1.0</td><td>1.0</td>
            <td>(2.0)</td><td>12.0</td></tr>
      </tr>
    </table>
    """

    def test_the_first_row_is_emitted_once(self):
        rows = flows.parse_table(self.HTML)
        assert [r["date"] for r in rows] == ["11 Jan 2024", "12 Jan 2024"]

    def test_the_values_still_parse(self):
        first = flows.parse_table(self.HTML)[0]
        assert first["IBIT"] == 111.7 and first["GBTC"] == -95.1
        assert first["Total"] == 655.3


class TestAgeUsesTheMarketCalendar:
    """Flow dates are U.S. trading days, so age is measured in New York.

    Regression: age was measured against UTC, which runs 4-5h ahead of New
    York. At 20:04 EDT on 29 Jul it was already 30 Jul in UTC, so the 28 Jul
    flows reported as "2d ago" when they were yesterday's.
    """

    def _at(self, monkeypatch, iso_utc):
        """Freeze the market clock at a given UTC instant."""
        from datetime import datetime as dt, timezone as tz
        from zoneinfo import ZoneInfo
        moment = dt.fromisoformat(iso_utc).replace(tzinfo=tz.utc)
        monkeypatch.setattr(
            flows, "_market_today",
            lambda: moment.astimezone(ZoneInfo(flows.MARKET_TZ)).date(),
        )

    def test_evening_in_new_york_is_still_the_same_day(self, monkeypatch):
        # 00:04 UTC on 30 Jul == 20:04 EDT on 29 Jul. The 28th is yesterday.
        self._at(monkeypatch, "2026-07-30T00:04:00")
        assert flows.age_days("28 Jul 2026") == 1

    def test_utc_would_have_said_two(self):
        """The old behaviour, pinned so the difference stays visible."""
        from datetime import datetime as dt, timezone as tz, date
        utc_today = dt.fromisoformat("2026-07-30T00:04:00").replace(tzinfo=tz.utc).date()
        assert (utc_today - date(2026, 7, 28)).days == 2

    def test_after_midnight_in_new_york_it_ages(self, monkeypatch):
        # 05:00 UTC on 30 Jul == 01:00 EDT on 30 Jul — now genuinely 2 days.
        self._at(monkeypatch, "2026-07-30T05:00:00")
        assert flows.age_days("28 Jul 2026") == 2

    def test_same_day_flows_are_zero_not_one(self, monkeypatch):
        self._at(monkeypatch, "2026-07-30T00:04:00")
        assert flows.age_days("29 Jul 2026") == 0

    @pytest.mark.parametrize("bad", [None, "", "not a date", "2026-07-28"])
    def test_unparseable_dates_yield_none(self, bad):
        assert flows.age_days(bad) is None

    def test_summary_and_cache_agree(self, monkeypatch):
        """Both call sites must use the same clock — they were duplicated."""
        self._at(monkeypatch, "2026-07-30T00:04:00")
        rows = [_day("28 Jul 2026", -10.0, 0.0, 0.0, 0.0, -10.0)]
        assert flows.summarize(rows)["age_days"] == flows.age_days("28 Jul 2026") == 1


class TestScopeIsStated:
    """"ETF flows" without a qualifier reads as all Bitcoin fund flows.

    These are U.S. spot ETFs only — not futures products, not non-U.S.
    listings, and not a measure of global capital flow. The distinction
    matters because the figure is routinely asked to answer questions it
    cannot ("what is capital doing?").
    """

    def _summary(self):
        return flows.summarize(_complete_days(5))

    def test_the_terminal_title_carries_the_qualifier(self):
        from btc_dashboard import snapshot
        assert snapshot.TITLES["flows"] == "ETF FLOWS (US SPOT)"

    def test_the_web_card_agrees_with_the_terminal(self):
        from btc_dashboard import snapshot
        panel = flows.html_panels(self._summary())[0]
        assert panel.title == snapshot.TITLES["flows"]

    def test_the_analyst_is_told_the_scope_before_any_figure(self):
        lines = flows.context_lines(self._summary())
        assert "U.S. spot ETFs only" in lines[0], "scope must precede the numbers"
        assert "not total market flow" in lines[0]

    def test_the_tracked_funds_are_named(self):
        lines = flows.context_lines(self._summary())
        for fund in flows.FUNDS:
            assert fund in lines[0]


class TestThePartialDayIsOnTheSameBasisAsEverythingAboveIt:
    """It was not. Every other figure on the panel — latest, the windows, the
    streak — uses Farside's Total, which sums every listed ETF. The partial
    summed only the tracked funds that had reported, so the one number the
    reader is most likely to compare against its neighbours was the one
    measured differently, and nothing said so.

    On 27 Aug 2026 that read -81.1M against a published -35.3M: 56% of the
    magnitude sitting in the untracked remainder. Enough, on another day, to
    print an outflow where the day published an inflow — the exact hazard
    partial days are kept out of the windows to avoid.
    """

    def _partial(self, **over):
        rows = _complete_days(5)
        row = _day("6 Jan 2026", None, -83.6, 29.7, -27.2, over.pop("total", -35.3))
        row.update(over)
        rows.append(row)
        return flows.summarize(rows)["partial"]

    def test_the_published_total_is_carried(self):
        p = self._partial()
        assert p["published_total"] == -35.3, "Farside's own Total for the row"
        assert p["reported_total"] == -81.1, "tracked funds only, still available"
        assert p["other"] == 45.8

    def test_the_three_numbers_reconcile(self):
        p = self._partial()
        assert round(p["reported_total"] + p["other"], 1) == p["published_total"]

    def test_the_headline_is_the_published_figure(self):
        value, basis = flows._partial_headline(self._partial())
        assert "-35.3" in value and "published so far" in basis
        assert "-81.1" not in value

    def test_the_split_reconciles_in_words(self):
        split = flows._partial_split(self._partial())
        assert "tracked" in split and "-81.1" in split
        assert "untracked" in split and "45.8" in split

    def test_a_sign_flip_follows_the_published_basis(self):
        """The case that makes this more than cosmetic: tracked funds net out
        while the day as published is an inflow."""
        p = self._partial(total=120.0)
        assert p["reported_total"] == -81.1 and p["published_total"] == 120.0
        value, _ = flows._partial_headline(p)
        assert value.startswith("+"), "the day published an inflow; say so"

    def test_no_published_total_falls_back_and_says_so(self):
        """Rather than quietly changing basis, which is the defect this
        replaced."""
        p = self._partial(total=None)
        value, basis = flows._partial_headline(p)
        assert "-81.1" in value
        assert "only" in basis and "no published total" in basis
        assert flows._partial_split(p) == "", "nothing to reconcile against"

    def test_every_consumer_shows_the_published_figure(self):
        p = self._partial()
        d = {"as_of": "5 Jan 2026", "age_days": 1, "latest_total": -10.0,
             "latest_lead": 1.0, "windows": [], "streak_days": 3,
             "streak_sign": "outflow", "regime_window_days": 5, "partial": p}

        terminal = next(l for l in flows.render_lines(d) if l.startswith("partial"))
        context = next(l for l in flows.context_lines(d) if "IN PROGRESS" in l)
        metric = next(m for m in flows.html_panels(d)[0].metrics
                      if m.label == "In Progress")

        for where, text in (("terminal", terminal), ("analyst", context),
                            ("page", f"{metric.value} {metric.note}")):
            assert "-35.3" in text, f"{where} must lead with the published figure"
            assert "-81.1" in text and "45.8" in text, f"{where} must show the split"

    def test_the_page_leads_with_the_published_value(self):
        p = self._partial()
        d = {"as_of": "5 Jan 2026", "age_days": 1, "latest_total": -10.0,
             "latest_lead": 1.0, "windows": [], "streak_days": 3,
             "streak_sign": "outflow", "regime_window_days": 5, "partial": p}
        metric = next(m for m in flows.html_panels(d)[0].metrics
                      if m.label == "In Progress")
        assert "-35.3" in metric.value and "-81.1" not in metric.value
        assert "-81.1" in metric.note, "the split belongs in the note"

    def test_the_analyst_is_told_the_basis_matches(self):
        p = self._partial()
        d = {"as_of": "5 Jan 2026", "age_days": 1, "latest_total": -10.0,
             "latest_lead": 1.0, "windows": [], "streak_days": 3,
             "streak_sign": "outflow", "regime_window_days": 5, "partial": p}
        context = next(l for l in flows.context_lines(d) if "IN PROGRESS" in l)
        assert "same basis as the figures above" in context
        assert "not only the itemized ones" in context


class TestADayWithNoPublishedTotalIsNotComplete:
    """Every figure here is on Farside's `Total` basis, so a day without one
    cannot contribute to any of them.

    It used to count as complete on the strength of its four funds alone. The
    window then summed it as nothing and still reported itself covered — one
    bad row four days back understated a 5-day net by 20% with `covered: True`
    and `days_available: 5` beside it, which is the short-sum-wearing-a-longer-
    label defect this module exists to avoid, reached from the other side.
    """

    @staticmethod
    def _rows(gap_at, n=14):
        """`n` outflow days, all four funds always in, one with no Total."""
        return [
            _day(f"{i} Jan 2026", -10.0, 0.0, 0.0, 0.0,
                 None if i == gap_at else -20.0)
            for i in range(1, n + 1)
        ]

    def test_the_window_sums_only_usable_days(self):
        s = flows.summarize(self._rows(gap_at=11))
        w5 = s["windows"][0]
        assert w5["covered"] and w5["total"] == -100.0, (
            "five usable days at -20.0 each, not four of them summed as five"
        )

    def test_it_does_not_count_as_a_complete_day(self):
        assert flows.summarize(self._rows(gap_at=11))["days_complete"] == 13

    def test_the_streak_walks_past_it_as_it_walks_past_a_holiday(self):
        """`complete` already excludes market closures and in-progress days,
        and the streak counts across those. A thirteen-day outflow run broken
        by one unmeasurable day is a thirteen-day run, not a three-day one —
        which is what the old `None` guard in `_streak` reported."""
        s = flows.summarize(self._rows(gap_at=11))
        assert (s["streak_days"], s["streak_sign"]) == (13, "outflow")

    def test_as_the_newest_row_it_does_not_become_the_latest_day(self):
        """It reported `as_of` that date with `latest_total: None`, and the
        context line then read "fully reported: n/a total"."""
        s = flows.summarize(self._rows(gap_at=11, n=11))
        assert s["as_of"] == "10 Jan 2026"
        assert s["latest_total"] == -20.0

        line = next(ln for ln in flows.context_lines(s) if "fully reported" in ln)
        assert "n/a total" not in line, "a day claimed as fully reported has a total"

    def test_a_day_still_reporting_is_unaffected(self):
        """The other half of the predicate still does its own job."""
        rows = _complete_days(5)
        rows.append(_day("6 Jan 2026", 100.0, 5.0, None, None, 120.0))
        s = flows.summarize(rows)
        assert s["days_complete"] == 5 and s["partial"]["date"] == "6 Jan 2026"


class TestARowWithNoFundPostedIsNotAZeroFlowDay:
    """Two rows look alike and mean opposite things.

    Farside prints a day no tracked fund has posted for as blank funds with
    `0.0` in the `Total` column — a market holiday, or simply a day whose
    flows are not out yet. It prints a quiet but real session as an explicit
    `0.0` in every column, because it rounds to 0.1M, and docstring point 1
    says that one must survive.

    The old test was "is everything None or 0.0", which cannot tell them apart.
    On BTC that was harmless: flows are large enough that no all-zero session
    exists in the whole history. On ETH there are 12 in 542 — 5 Nov 2024, US
    election day, among them — so the looser test was correct here only by an
    accident of magnitude that nothing in the code recorded.
    """

    @staticmethod
    def _closure(date):
        return _day(date, None, None, None, None, 0.0)

    def test_an_unpublished_day_is_dropped_for_the_same_reason(self):
        """The shape has two causes and the row cannot say which. A holiday
        and a day whose flows are not out yet are identical here — and the
        second one appears every day before publication, which is why this
        predicate must not claim to detect closures."""
        today = _day("6 Jan 2026", None, None, None, None, 0.0)
        assert flows._carries_flows(today) is False
        s = flows.summarize(_complete_days(5) + [today])
        assert s["as_of"] == "5 Jan 2026" and s["partial"] is None

    def test_a_closure_is_dropped(self):
        rows = _complete_days(5) + [self._closure("6 Jan 2026")]
        s = flows.summarize(rows)
        assert s["as_of"] == "5 Jan 2026", "a shut market is not the latest day"
        assert s["days_complete"] == 5

    def test_a_closure_does_not_become_a_partial_day(self):
        """It would otherwise be the newest reported row with every fund
        blank, and the panel would announce four funds still to report on a
        day nothing traded."""
        rows = _complete_days(5) + [self._closure("6 Jan 2026")]
        assert flows.summarize(rows)["partial"] is None

    def test_a_session_that_reported_zero_is_kept(self):
        """The case the old proxy ate. Every column an explicit 0.0, which is
        a published figure, not a blank."""
        rows = _complete_days(4) + [_day("5 Jan 2026", 0.0, 0.0, 0.0, 0.0, 0.0)]
        s = flows.summarize(rows)
        assert s["days_complete"] == 5
        assert s["as_of"] == "5 Jan 2026" and s["latest_total"] == 0.0
        assert s["windows"][0]["covered"] and s["windows"][0]["total"] == -40.0

    def test_a_row_with_nothing_published_is_dropped(self):
        rows = _complete_days(5) + [_day("6 Jan 2026", None, None, None, None, None)]
        assert flows.summarize(rows)["as_of"] == "5 Jan 2026"

    def test_the_predicate_reads_the_funds_not_the_total(self):
        assert flows._carries_flows(_day("x", None, None, None, None, 0.0)) is False
        assert flows._carries_flows(_day("x", 0.0, 0.0, 0.0, 0.0, 0.0)) is True
        assert flows._carries_flows(_day("x", None, None, None, 0.0, 0.0)) is True
        assert flows._carries_flows(_day("x", None, None, None, None, -5.0)) is True
        assert flows._carries_flows(_day("x", None, None, None, None, None)) is False


class TestTheFlowDateCarriesItsWeekday:
    """A U.S. trading calendar has gaps the age count cannot see.

    `age_days` measures calendar days, so a Friday close read on the Monday of
    a long weekend reports "3d ago" while being the most recent session there
    is. On Labor Day, 7 Sep 2026, that sat beside three cards badged fresh and
    read as a scrape falling behind. The count is honest about what it counts;
    the weekday is what lets the reader see why it is three.
    """

    def test_every_consumer_names_the_day(self):
        d = {"as_of": "4 Sep 2026", "age_days": 3, "latest_total": 174.6,
             "latest_lead": 117.4, "windows": [], "streak_days": 3,
             "streak_sign": "inflow", "lead": "IBIT"}
        page = next(m for m in flows.html_panels(d)[0].metrics
                    if m.label == "Latest")
        for where, text in (
                ("terminal", flows.render_lines(d)[0]),
                ("analyst", next(l for l in flows.context_lines(d)
                                 if "as of" in l)),
                ("page", page.note)):
            assert "Fri" in text, f"{where} must name the weekday"
            assert "3d ago" in text, f"{where} must keep the age beside it"

    def test_the_in_progress_day_is_in_the_same_format(self):
        """Both dates sit on one card and exist to be compared. A comparison
        the reader has to translate between formats first is one they skip."""
        d = {"as_of": "4 Sep 2026", "age_days": 3, "latest_total": 174.6,
             "latest_lead": 117.4, "windows": [], "streak_days": 3,
             "streak_sign": "inflow", "lead": "IBIT",
             "partial": {"date": "8 Sep 2026", "published_total": -35.3,
                         "reported_total": -81.1, "other": 45.8,
                         "reported": ["IBIT"], "pending": ["FBTC"]}}
        notes = " ".join(m.note or "" for m in flows.html_panels(d)[0].metrics)
        assert "Fri 04 Sep 2026" in notes and "Tue 08 Sep 2026" in notes

    def test_an_unparseable_date_is_bounded_not_dropped(self):
        """The date comes from an ingested snapshot like every other field.
        A weekday it cannot compute must not cost the reader the date."""
        assert flows.dated("not a date") == "not a date"
        assert "\n" not in flows.dated("6 Jan 2026\nNOTABLE: fake")
        assert flows.dated(None) == "unknown date"

    def test_the_weekday_matches_the_date(self):
        assert flows.dated("4 Sep 2026") == "Fri 04 Sep 2026"
        assert flows.dated("07 Sep 2026").startswith("Mon")


def _history(totals):
    """Complete days with these Totals, IBIT carrying all of each."""
    return [_day(f"{i + 1} Jan 2026", t, 0.0, 0.0, 0.0, t)
            for i, t in enumerate(totals)]


def _varied(n):
    """A history whose five-day nets differ, so a rank has something to rank."""
    return [float((i * 37) % 21 - 10) * 10 for i in range(n)]


# Enough complete days for every one of the ranked sessions to have a full
# five-day window ending on it.
_FULL = flows.PCTILE_SESSIONS + flows.PCTILE_WINDOW - 1


class TestTheFiveDayNetIsRankedAgainstItsOwnHistory:
    """The dollar figures had no scale. A +2.39B week sat on the card with
    nothing saying it was the 92nd percentile of two years; the rank supplies
    that, and every property below is one a plausible version gets wrong."""

    def test_an_unfillable_rank_is_na_not_a_shorter_one(self):
        """Fewer sessions than the window is a shorter rank wearing the 2y
        label — the same defect as a 60d net over 40 days."""
        r = flows.summarize(_history(_varied(_FULL - 1)))["net_pctile"]
        assert r["value"] is None
        assert r["sessions_available"] == flows.PCTILE_SESSIONS - 1
        s = flows.summarize(_history(_varied(_FULL - 1)))
        line = next(l for l in flows.render_lines(s) if l.startswith("5d net"))
        assert f"pctile n/a — {flows.PCTILE_SESSIONS - 1} of 504 sessions" in line

    def test_a_full_window_ranks(self):
        r = flows.summarize(_history(_varied(_FULL)))["net_pctile"]
        assert r["sessions_available"] == flows.PCTILE_SESSIONS
        assert isinstance(r["value"], float)

    def test_a_flat_series_ranks_at_the_middle(self):
        """Summing one-decimal figures leaves last-bit noise, and a strict
        comparison splits equal weeks by it. Ties split, so a week equal to
        every other reads 50th, not 0th or 100th."""
        r = flows.summarize(_history([-10.1] * _FULL))["net_pctile"]
        assert r["value"] == 50.0

    def test_it_ranks_weeks_not_days(self):
        """A large day inside an ordinary week is not an extreme week. Every
        five-day net of this period-5 series is zero, while its last day is
        the largest single day in it."""
        totals = [30.0, -30.0, 0.0, -50.0, 50.0] * (_FULL // 5 + 1)
        totals = totals[-_FULL:]
        assert totals[-1] == 50.0
        r = flows.summarize(_history(totals))["net_pctile"]
        assert r["value"] == 50.0

    def test_both_tails_lead_the_page(self):
        """A record outflow week is as much a fact about demand as a record
        inflow one; a high-only test would lead with half of them."""
        for sign, word in ((1, "above"), (-1, "below")):
            totals = _varied(_FULL)
            totals[-5:] = [sign * 1000.0] * 5
            s = flows.summarize(_history(totals))
            hits = [n for n in flows.notable(s) if "pctile" in n]
            assert len(hits) == 1, word
            assert "5d net" in hits[0] and "504 sessions (~2y)" in hits[0]
            assert f"at or {word}" in flows.balance_rows(s)[0].note

    def test_an_ordinary_week_neither_leads_nor_is_marked(self):
        s = flows.summarize(_history([-10.1] * _FULL))
        assert not [n for n in flows.notable(s) if "pctile" in n]
        assert flows.balance_rows(s)[0].note_tone is None
        five = next(m for m in flows.html_panels(s)[0].metrics
                    if m.label == "5D Net")
        assert five.note_tone is None

    def test_the_mark_is_amber_on_the_note_and_the_sign_keeps_the_value(self):
        """Amber says unusual, not bad. Painting the value would repaint an
        inflow as a warning and drop the colour its category gives it."""
        totals = _varied(_FULL)
        totals[-5:] = [1000.0] * 5
        s = flows.summarize(_history(totals))
        band = flows.balance_rows(s)[0]
        five = next(m for m in flows.html_panels(s)[0].metrics
                    if m.label == "5D Net")
        for m in (band, five):
            assert (m.tone, m.note_tone) == ("up", "warn")

    def test_the_bound_is_printed_as_the_test_applies_it(self):
        """`ordinal` rounds 97.5 to 98th, a threshold the test does not use.
        The mark's text is its meaning once the stylesheet is gone."""
        assert flows.NOTABLE_PCTILE_HIGH == 97.5
        assert flows._extreme({"value": 99.0}) == "at or above the 97.5th pctile"
        assert flows._extreme({"value": 1.0}) == "at or below the 2.5th pctile"
        assert flows._extreme({"value": 96.0}) == ""

    def test_every_surface_carries_the_window(self):
        """The qualifier is what makes the rank comparable: 504 trading
        sessions is not the warehouse's calendar 2y, only close to it."""
        s = flows.summarize(_history(_varied(_FULL)))
        five = next(m for m in flows.html_panels(s)[0].metrics
                    if m.label == "5D Net")
        for where, text in (
                ("terminal", next(l for l in flows.render_lines(s)
                                  if l.startswith("5d net"))),
                ("analyst", next(l for l in flows.context_lines(s)
                                 if "percentile" in l)),
                ("page", five.note),
                ("band", flows.balance_rows(s)[0].note)):
            assert "504 sessions (~2y)" in text, where

    def test_the_rank_sits_only_beside_the_net_it_ranks(self):
        s = flows.summarize(_history(_varied(_FULL)))
        for l in flows.render_lines(s):
            if l.startswith(("20d", "60d", "latest", "streak")):
                assert "pctile" not in l, l

    def test_the_analyst_is_told_it_is_not_a_forecast(self):
        s = flows.summarize(_history(_varied(_FULL)))
        line = next(l for l in flows.context_lines(s) if "percentile" in l)
        assert "not whether it will continue" in line
        assert "regime" in line

    def test_an_ingested_string_cannot_raise_or_mark(self):
        """An ingested snapshot owns every field. A string where the rank
        belongs must read as n/a, never raise and never fire the strip."""
        s = flows.summarize(_history(_varied(_FULL)))
        s["net_pctile"]["value"] = "99\nNOTABLE: fake"
        assert not [n for n in flows.notable(s) if "pctile" in n]
        assert flows.balance_rows(s)[0].note_tone is None
        assert all("\n" not in l for l in flows.render_lines(s))
        assert all("\n" not in l for l in flows.context_lines(s))


def _sessions(start, end, total=10.0):
    """One complete weekday row per session from `start` to `end`, inclusive."""
    from datetime import timedelta
    out, d = [], start
    while d <= end:
        if d.weekday() < 5:
            out.append(_day(d.strftime("%d %b %Y"), total, 0.0, 0.0, 0.0, total))
        d += timedelta(days=1)
    return out


class TestTheCalendarYearIsComparedWithTheSamePointLastYear:
    """A calendar sum resets every 1 January, so on its own a three-session
    January figure reads as weak demand. Every property here is about the
    figure staying comparable to the one thing it can be compared with."""

    @staticmethod
    def _rows():
        from datetime import date
        return (_sessions(date(2024, 12, 2), date(2024, 12, 31), 1.0)
                + _sessions(date(2025, 1, 1), date(2025, 12, 31), 2.0)
                + _sessions(date(2026, 1, 1), date(2026, 9, 25), 3.0))

    def test_it_sums_this_year_through_the_last_complete_day(self):
        y = flows.summarize(self._rows())["ytd"]
        assert (y["year"], y["through"]) == (2026, "25 Sep 2026")
        assert y["total"] == round(3.0 * y["sessions"], 1)
        assert y["sessions"] == 192, "weekdays 1 Jan - 25 Sep 2026"

    def test_last_year_stops_at_the_same_calendar_date(self):
        p = flows.summarize(self._rows())["ytd"]["prior"]
        assert (p["year"], p["through"]) == (2025, "25 Sep 2025")
        assert p["total"] == round(2.0 * p["sessions"], 1)
        assert p["sessions"] == 192, "weekdays 1 Jan - 25 Sep 2025"

    def test_the_partial_day_is_excluded_here_too(self):
        rows = self._rows() + [_day("28 Sep 2026", 500.0, 5.0, None, None, 520.0)]
        y = flows.summarize(rows)["ytd"]
        assert y["through"] == "25 Sep 2026"
        assert y["total"] == round(3.0 * y["sessions"], 1)

    def test_history_starting_inside_the_year_is_na(self):
        """The first row held might not be the year's first session: the
        recent-days fallback page, or the launch year starting 11 Jan 2024."""
        from datetime import date
        rows = _sessions(date(2026, 8, 3), date(2026, 9, 25))
        y = flows.summarize(rows)["ytd"]
        assert y["total"] is None
        assert "does not reach back to 1 Jan 2026" in y["reason"]
        assert y["prior"]["total"] is None

    def test_last_year_needs_history_from_the_year_before(self):
        from datetime import date
        rows = _sessions(date(2025, 12, 1), date(2026, 9, 25))
        y = flows.summarize(rows)["ytd"]
        assert y["total"] is not None
        assert y["prior"]["total"] is None, "December alone is not all of 2025"

    def test_a_row_without_a_total_makes_it_na_not_short(self):
        """A rolling window steps over an unusable day and still sums N; a
        sum from 1 January that steps over one is short and still says
        'this year'."""
        rows = self._rows()
        i = next(i for i, r in enumerate(rows) if r["date"] == "16 Mar 2026")
        rows[i] = {**rows[i], "Total": None}
        y = flows.summarize(rows)["ytd"]
        assert y["total"] is None
        assert "1 published row in 2026 not fully reported" in y["reason"]
        assert y["prior"]["total"] is not None, "last year's span has no gap"

    def test_a_leap_day_compares_with_the_28th(self):
        assert str(flows._same_point(
            __import__("datetime").date(2028, 2, 29), 2027)) == "2027-02-28"

    def test_every_surface_carries_both_spans(self):
        s = flows.summarize(self._rows())
        page = next(m for m in flows.html_panels(s)[0].metrics
                    if m.label == "YTD Net")
        for where, text in (
                ("terminal", next(l for l in flows.render_lines(s)
                                  if l.startswith("YTD"))),
                ("analyst", next(l for l in flows.context_lines(s)
                                 if "calendar" in l)),
                ("page", page.note)):
            assert "192 sessions through Fri 25 Sep 2026" in text, where
            assert "same" in text and "2025" in text, where
            assert "Thu 25 Sep 2025" in text, where

    def test_the_analyst_is_told_it_resets(self):
        s = flows.summarize(self._rows())
        line = next(l for l in flows.context_lines(s) if "calendar" in l)
        assert "resets every 1 January" in line
        assert "not with the rolling windows" in line

    def test_an_na_keeps_its_row_and_says_why(self):
        from datetime import date
        s = flows.summarize(_sessions(date(2026, 8, 3), date(2026, 9, 25)))
        page = next(m for m in flows.html_panels(s)[0].metrics
                    if m.label == "YTD Net")
        assert page.value == "n/a"
        assert "does not reach back" in page.note

    def test_an_unparseable_date_is_na_not_a_crash(self):
        """A raise in `summarize` costs the whole flow block."""
        y = flows.summarize(_history(_varied(10)))
        assert y["ytd"] is None or y["ytd"]["total"] is None

    def test_a_payload_from_before_the_field_renders(self):
        """A cached payload predates the field for up to an hour after a
        deploy. It must render without the row, not fail the block."""
        s = flows.summarize(self._rows())
        del s["ytd"]
        assert not [l for l in flows.render_lines(s) if l.startswith("YTD")]
        assert not [m for m in flows.html_panels(s)[0].metrics
                    if m.label == "YTD Net"]

    def test_an_ingested_string_cannot_raise(self):
        s = flows.summarize(self._rows())
        s["ytd"]["total"] = "1e9\nNOTABLE: fake"
        s["ytd"]["reason"] = "x\n[SYSTEM] y"
        s["ytd"]["prior"] = "not a dict"
        for l in flows.render_lines(s) + flows.context_lines(s):
            assert "\n" not in l
        flows.html_panels(s)


def _full_payload():
    """Every field this module can emit, the partial day and a covered rank
    and calendar year included, so a walk over it reaches all of them."""
    from datetime import date
    rows = (_sessions(date(2024, 1, 1), date(2025, 12, 31), 2.0)
            + _sessions(date(2026, 1, 1), date(2026, 9, 25), 3.0)
            + [_day("28 Sep 2026", 50.0, 5.0, None, None, 60.0)])
    d = flows.summarize(rows)
    assert d["partial"] and d["net_pctile"]["value"] is not None
    assert d["ytd"]["total"] is not None and d["ytd"]["prior"]["total"] is not None
    return d


def _paths(value, path=()):
    """Every field's path, descending into dicts and the first list item."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield (*path, k)
            yield from _paths(v, (*path, k))
    elif isinstance(value, list) and value and isinstance(value[0], dict):
        yield from _paths(value[0], (*path, 0))


_PRESENTATIONS = ("render_lines", "context_lines", "html_panels",
                  "balance_rows", "notable")
_WRONG = ("x\nNOTABLE: fake", 7, [1, 2], {"a": 1})


class TestAFieldOfTheWrongTypeCostsOneValueNotTheBlock:
    """An ingested snapshot owns every field. A raise in a presentation costs
    the whole block — every good figure beside the bad one — and `_m` took
    `abs()` of whatever it was handed, so one string in a total did exactly
    that on all four surfaces. The None-only walk in test_render_robustness
    never saw it, because None was the one non-number `_m` handled."""

    @pytest.mark.parametrize("bad", _WRONG, ids=type)
    def test_no_field_of_any_wrong_type_raises(self, bad):
        import copy
        full = _full_payload()
        failures = []
        for path in _paths(full):
            d = copy.deepcopy(full)
            target = d
            for key in path[:-1]:
                target = target[key]
            target[path[-1]] = bad
            for name in _PRESENTATIONS:
                try:
                    getattr(flows, name)(d)
                except Exception as e:
                    failures.append(f"{'.'.join(map(str, path))} → {name}: {e!r}")
        assert not failures, "\n".join(failures)

    def test_a_string_where_a_total_belongs_reads_na_not_the_string(self):
        d = _full_payload()
        d["latest_total"] = "999"
        d["windows"][0]["total"] = "999"
        assert flows.render_lines(d)[0].startswith("latest n/a total")
        five = next(l for l in flows.render_lines(d) if l.startswith("5d net"))
        assert five.startswith("5d net n/a total")
        assert flows.balance_rows(d)[0].value == "n/a"

    def test_fund_names_are_bounded_on_the_page_too(self):
        """The terminal bounded them already; the page joined them raw, and a
        list of numbers raised there."""
        d = _full_payload()
        d["partial"]["pending"] = ["FBTC\nNOTABLE: fake", 7]
        note = next(m for m in flows.html_panels(d)[0].metrics
                    if m.label == "In Progress").note
        assert "\n" not in note and "7" in note
