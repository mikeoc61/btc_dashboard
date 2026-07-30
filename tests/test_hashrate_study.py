"""Analysis primitives for tools/hashrate_study.py.

These test the maths against series with known answers, so a refactor cannot
quietly change what the study reports. The study's conclusions depend entirely
on these being right — a moving average off by one row, or an episode detector
that splits one drawdown into three, would change every table it prints.
"""
from __future__ import annotations

import datetime

import pytest

hashrate_study = pytest.importorskip("hashrate_study")


class TestMovingAverage:
    def test_pads_until_the_window_is_full(self):
        ma = hashrate_study.moving_average([1, 2, 3, 4, 5], 3)
        assert ma[:2] == [None, None]
        assert ma[2:] == [2.0, 3.0, 4.0]

    def test_partial_windows_are_never_emitted(self):
        """A 3-day mean and a 60-day mean are different statistics; letting a
        partial window through would put them on the same axis."""
        ma = hashrate_study.moving_average(list(range(10)), 60)
        assert all(v is None for v in ma)

    def test_matches_a_naive_implementation(self):
        xs = [3.0, 1.0, 4.0, 1.0, 5.0, 9.0, 2.0, 6.0]
        rolling = hashrate_study.moving_average(xs, 4)
        for i in range(3, len(xs)):
            assert rolling[i] == pytest.approx(sum(xs[i - 3:i + 1]) / 4)


class TestDrawdownFromHigh:
    def test_zero_at_a_new_high(self):
        assert hashrate_study.drawdown_from_high([1, 2, 3], 90)[-1] == 0.0

    def test_measures_against_the_window_peak(self):
        dd = hashrate_study.drawdown_from_high([100, 100, 70], 90)
        assert dd[-1] == pytest.approx(-30.0)

    def test_peak_leaves_the_window(self):
        """Once the old high ages out, the drawdown recovers even if the value
        has not — the window is trailing, not all-time."""
        xs = [100.0] + [50.0] * 5
        assert hashrate_study.drawdown_from_high(xs, 3)[-1] == pytest.approx(0.0)


class TestRibbonSignals:
    def test_fires_on_the_cross_not_the_state(self):
        # Fall for 90 days then recover: exactly one crossing back above.
        hr = [100 - i for i in range(90)] + [10 + i * 3 for i in range(90)]
        assert len(hashrate_study.ribbon_signals(hr, fast=5, slow=10)) == 1

    def test_a_flat_series_never_fires(self):
        assert hashrate_study.ribbon_signals([100.0] * 300, 30, 60) == []

    def test_returns_indices_into_the_series(self):
        hr = [100 - i for i in range(90)] + [10 + i * 3 for i in range(90)]
        for i in hashrate_study.ribbon_signals(hr, fast=5, slow=10):
            assert 0 <= i < len(hr)


class TestForwardReturn:
    def test_computes_percentage_change(self):
        assert hashrate_study.forward_return([100.0, 0, 150.0], 0, 2) == pytest.approx(50.0)

    def test_none_past_the_end(self):
        assert hashrate_study.forward_return([100.0, 110.0], 0, 5) is None


class TestDrawdownEpisodes:
    def _series(self, closes, start=datetime.date(2020, 1, 1)):
        return [start + datetime.timedelta(days=i) for i in range(len(closes))], closes

    def test_detects_one_episode_and_its_trough(self):
        dates, closes = self._series([100, 90, 70, 50, 70, 95, 100])
        eps = hashrate_study.drawdown_episodes(dates, closes, min_depth=25)
        assert len(eps) == 1
        assert eps[0].depth_pct == pytest.approx(-50.0)
        assert eps[0].trough_date == dates[3]
        assert eps[0].resolved

    def test_recovery_band_stops_one_dip_becoming_many(self):
        """Oscillating either side of the entry threshold is one episode."""
        dates, closes = self._series([100, 70, 76, 70, 76, 70, 95, 100])
        eps = hashrate_study.drawdown_episodes(dates, closes, min_depth=25)
        assert len(eps) == 1

    def test_unrecovered_episode_is_marked_unresolved(self):
        dates, closes = self._series([100, 90, 60, 55, 58])
        eps = hashrate_study.drawdown_episodes(dates, closes, min_depth=25)
        assert len(eps) == 1
        assert eps[0].recovered is None and eps[0].resolved is False

    def test_shallow_dips_are_ignored(self):
        dates, closes = self._series([100, 95, 90, 95, 100])
        assert hashrate_study.drawdown_episodes(dates, closes, min_depth=25) == []

    def test_era_is_labelled_by_etf_launch(self):
        pre = self._series([100, 50, 100], datetime.date(2020, 1, 1))
        post = self._series([100, 50, 100], datetime.date(2025, 1, 1))
        assert hashrate_study.drawdown_episodes(*pre, min_depth=25)[0].era == "pre-ETF"
        assert hashrate_study.drawdown_episodes(*post, min_depth=25)[0].era == "ETF"


class TestScoring:
    def test_edge_is_measured_against_the_base_rate(self):
        """A signal that fires on ordinary days must show no edge, however
        large its absolute return — the trap the base column exists to catch."""
        close = [100.0 * (1.02 ** i) for i in range(400)]   # relentless uptrend
        every_tenth = list(range(0, 300, 10))
        stats = hashrate_study.score(close, every_tenth, (90,))[0]
        assert stats.signal_median > 100, "absolute return is large..."
        assert abs(stats.edge_pp) < 25, "...but the edge over base is not"

    def test_effective_n_collapses_clustered_signals(self):
        """Ten signals inside one horizon are close to a single bet."""
        clustered = list(range(100, 110))
        assert hashrate_study._effective_n(clustered, 365) == 1
        spread = [0, 400, 800]
        assert hashrate_study._effective_n(spread, 365) == 3

    def test_no_signals_yields_empty_stats(self):
        stats = hashrate_study.score([100.0] * 500, [], (90,))[0]
        assert stats.signal_median is None and stats.n_signals == 0


class TestDecileTable:
    def test_orders_lowest_metric_first(self):
        metric = [float(i) for i in range(100)]
        close = [100.0] * 200
        rows = hashrate_study.decile_table(metric, close, (10,), k=10)
        assert len(rows) == 10
        assert rows[0]["low"] <= rows[0]["high"] <= rows[-1]["low"]

    def test_reports_effective_sample_beside_the_count(self):
        metric = [float(i) for i in range(1000)]
        close = [100.0] * 1400
        row = hashrate_study.decile_table(metric, close, (365,), k=10)[0]
        assert row["n"] == 100
        # 100 consecutive days hold well under one independent 365d window.
        assert row["effective_n"] < 1

    def test_survives_a_series_shorter_than_the_horizon(self):
        rows = hashrate_study.decile_table([1.0, 2.0], [100.0, 101.0], (365,), k=2)
        assert all(r["returns"][365] is None for r in rows)


class TestPercentileOf:
    def test_share_strictly_below(self):
        assert hashrate_study.percentile_of([1.0, 2.0, 3.0, 4.0], 3.0) == 50.0

    def test_empty_is_zero_not_an_error(self):
        assert hashrate_study.percentile_of([], 1.0) == 0.0


class TestCLI:
    def test_rejects_bad_horizons(self, capsys):
        assert hashrate_study.main(["--horizons", "abc"]) == 2
        assert "bad --horizons" in capsys.readouterr().err

    def test_rejects_empty_horizons(self, capsys):
        assert hashrate_study.main(["--horizons", ","]) == 2

    def test_missing_warehouse_exits_nonzero(self, tmp_path, capsys):
        rc = hashrate_study.main(["--db", str(tmp_path / "nope.duckdb")])
        assert rc == 1
        assert "could not read the warehouse" in capsys.readouterr().err


class TestSignalAssociationIsBounded:
    """A signal a year from a trough is not 'its' signal.

    Unbounded nearest-match reported lags of -140d to -386d for the 2017
    corrections, implying a relationship where there was only a long gap.
    """

    def test_cap_is_conservative(self):
        assert hashrate_study.MAX_ASSOCIATION_DAYS <= 90

    def _run(self, gap_days):
        import datetime
        n = 800
        dates = [datetime.date(2020, 1, 1) + datetime.timedelta(days=i) for i in range(n)]
        # Price troughs at index 400; hashrate capitulates and recovers near
        # index 400 + gap_days.
        close = [100.0] * n
        for i in range(300, 401):
            close[i] = 100.0 - (i - 300) * 0.6
        for i in range(401, n):
            close[i] = 40.0 + (i - 401) * 0.6
        peak = 400 + gap_days
        hr = [100.0] * n
        for i in range(max(0, peak - 100), peak):
            hr[i] = 100.0 - (peak - i)
        s = hashrate_study.Series(
            dates=dates, hashrate=hr, close=close,
            price_dates=dates, price_close=close,
        )
        return hashrate_study.analyse(s, (90,), 25.0)

    def test_a_distant_signal_is_not_attributed(self):
        troughs = self._run(gap_days=300)["troughs"]
        assert troughs, "expected a detected episode"
        assert all(t["signal_lag_days"] is None for t in troughs)
        assert all(t["nearest_signal"] is None for t in troughs)

    def test_a_nearby_signal_is_attributed(self):
        troughs = self._run(gap_days=10)["troughs"]
        assert troughs
        assert any(t["signal_lag_days"] is not None for t in troughs)
