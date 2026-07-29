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
    def test_lead_dominant_is_conviction(self):
        assert flows.classify(-100.0, -80.0) == "conviction"

    def test_lead_exceeding_total_is_offsetting(self):
        assert flows.classify(-100.0, -150.0) == "offsetting"

    def test_lead_minority_is_broad(self):
        assert flows.classify(-100.0, -20.0) == "broad"

    def test_lead_against_total_is_flagged(self):
        assert flows.classify(-100.0, 40.0) == "lead opposing"

    def test_no_total_is_unclassified(self):
        assert flows.classify(None, -10.0) is None
        assert flows.classify(0.0, 0.0) is None


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
