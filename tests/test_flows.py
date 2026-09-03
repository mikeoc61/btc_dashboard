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
