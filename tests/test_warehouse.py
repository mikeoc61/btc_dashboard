"""Warehouse query layer, against a real DuckDB file built for the test.

These exercise the SQL, not a mock — the signal definitions are the part most
worth pinning down, and a mock would only assert that we called ourselves.
"""
from __future__ import annotations

import datetime

import pytest

duckdb = pytest.importorskip("duckdb")

from btc_dashboard.config import Config
from btc_dashboard.sources import warehouse


def _utc_today() -> datetime.date:
    """The warehouse buckets by UTC calendar day, so tests must anchor to UTC.

    Using local date().today() here made these tests fail during the hours when
    the local date and the UTC date differ.
    """
    return datetime.datetime.now(datetime.timezone.utc).date()


def _build_db(path, days=400, fee=lambda i: 2.0, hashrate=lambda i: 800.0,
              vol=lambda i: 1000.0, close=lambda i: 90000.0, end_date=None):
    """Write a synthetic warehouse ending on `end_date` (default today), oldest first."""
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE onchain (date DATE PRIMARY KEY, hash_rate_ehs DOUBLE, "
        "difficulty_t DOUBLE, blocks_day INTEGER, block_fullness DOUBLE, "
        "p50_fee DOUBLE, miner_rev DOUBLE, fee_subsidy DOUBLE, tx_rate DOUBLE, "
        "retarget_proj DOUBLE)"
    )
    con.execute(
        "CREATE TABLE btc (date DATE PRIMARY KEY, close DOUBLE, "
        "kraken_vol DOUBLE, kraken_trades BIGINT)"
    )
    today = end_date or _utc_today()
    for i in range(days):
        d = today - datetime.timedelta(days=days - 1 - i)
        con.execute(
            "INSERT INTO onchain VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [d, hashrate(i), 100.0, 144, 95.0, 2.0, 450.0, fee(i), 6.5, 0.0],
        )
        con.execute("INSERT INTO btc VALUES (?, ?, ?, ?)", [d, close(i), vol(i), 5000])
    con.close()
    return path


@pytest.fixture
def db(tmp_path):
    return _build_db(tmp_path / "market.duckdb")


def _con(path):
    return duckdb.connect(str(path), read_only=True)


class TestSignals:
    def test_percentile_puts_a_new_low_at_the_bottom(self, tmp_path):
        # Flat history, then today collapses.
        path = _build_db(
            tmp_path / "m.duckdb", days=400,
            fee=lambda i: 0.1 if i == 399 else 2.0,
        )
        con = _con(path)
        try:
            pct = warehouse.percentile_rank(con, "fee_subsidy", window_days=730, smooth_days=1)
        finally:
            con.close()
        # A unique minimum: nothing below it, and it ties only with itself, so
        # mid-ranking puts it a half-observation above the floor.
        assert pct == pytest.approx(0.125)

    def test_a_flat_series_ranks_in_the_middle(self, db):
        """Every day identical — the honest answer is 50th, not 0th or 100th.

        Guards the tie handling: the smoothed window means are equal only to
        within floating-point noise, and a strict comparison ranks them
        arbitrarily.
        """
        con = _con(db)
        try:
            assert warehouse.percentile_rank(
                con, "fee_subsidy", smooth_days=7
            ) == pytest.approx(50.0)
        finally:
            con.close()

    def test_percentile_returns_none_below_minimum_rows(self, tmp_path):
        path = _build_db(tmp_path / "m.duckdb", days=5)
        con = _con(path)
        try:
            assert warehouse.percentile_rank(con, "fee_subsidy", window_days=730) is None
        finally:
            con.close()

    def test_smoothing_cancels_a_weekend_cycle(self, tmp_path):
        """Nothing changes week to week except the weekday — the raw percentile
        should still move, and the smoothed one should not.

        fee_subsidy runs ~27% lower at weekends. With a purely seasonal series,
        a weekday's raw percentile lands at roughly the weekend share of the
        window (~2/7) purely because weekends sit below it, and a weekend's
        lands at 0 — a 28-point swing that reports the calendar, not the
        network. Anchored to a fixed Wednesday so the result doesn't depend on
        the day the suite runs.
        """
        WED = datetime.date(2026, 7, 29)
        SAT = datetime.date(2026, 8, 1)

        def seasonal(end):
            def fee(i):
                d = end - datetime.timedelta(days=399 - i)
                return 1.46 if d.isoweekday() >= 6 else 2.0
            return fee

        results = {}
        for label, end in (("weekday", WED), ("weekend", SAT)):
            path = _build_db(
                tmp_path / f"{label}.duckdb", days=400,
                fee=seasonal(end), end_date=end,
            )
            con = _con(path)
            try:
                results[label] = (
                    warehouse.percentile_rank(con, "fee_subsidy", smooth_days=1),
                    warehouse.percentile_rank(con, "fee_subsidy", smooth_days=7),
                )
            finally:
                con.close()

        raw_wed, smooth_wed = results["weekday"]
        raw_sat, smooth_sat = results["weekend"]

        # Raw: identical network conditions, tens of percentile points apart —
        # the reading is dominated by which day of the week it happens to be.
        assert raw_wed - raw_sat > 25

        # Smoothed: every 7-day window spans one of each weekday, so the cycle
        # cancels and the reading barely moves. Not bit-identical — the handful
        # of partial windows at the start of the series depends on the weekday
        # the series opens on — but within a couple of points either way.
        assert abs(smooth_wed - smooth_sat) < 2

    def test_drawdown_measures_against_the_window_high(self, tmp_path):
        path = _build_db(
            tmp_path / "m.duckdb", days=400,
            hashrate=lambda i: 1000.0 if i < 399 else 700.0,
        )
        con = _con(path)
        try:
            dd = warehouse.drawdown_from_high(con, "hash_rate_ehs", window_days=90)
        finally:
            con.close()
        assert dd == pytest.approx(-30.0)

    def test_drawdown_is_zero_at_a_new_high(self, db):
        con = _con(db)
        try:
            assert warehouse.drawdown_from_high(con, "hash_rate_ehs") == pytest.approx(0.0)
        finally:
            con.close()

    def test_apathy_streak_counts_back_from_today(self, tmp_path):
        path = _build_db(
            tmp_path / "m.duckdb", days=400,
            fee=lambda i: 0.5 if i >= 397 else 2.0,
        )
        con = _con(path)
        try:
            assert warehouse.apathy_streak(con) == 3
        finally:
            con.close()

    def test_apathy_streak_uses_an_absolute_threshold(self, tmp_path):
        # Uniformly depressed history: a percentile would show nothing unusual,
        # the absolute threshold correctly reports a long regime.
        path = _build_db(tmp_path / "m.duckdb", days=400, fee=lambda i: 0.5)
        con = _con(path)
        try:
            assert warehouse.apathy_streak(con) == 400
        finally:
            con.close()

    def test_sma200_needs_a_full_window(self, tmp_path):
        short = _build_db(tmp_path / "short.duckdb", days=100)
        con = _con(short)
        try:
            assert warehouse.sma200(con) == (None, None)
        finally:
            con.close()

    def test_sma200_and_pct(self, tmp_path):
        path = _build_db(
            tmp_path / "m.duckdb", days=400,
            close=lambda i: 110000.0 if i == 399 else 100000.0,
        )
        con = _con(path)
        try:
            sma, pct = warehouse.sma200(con)
        finally:
            con.close()
        # 199 days at 100k plus today at 110k.
        assert sma == pytest.approx(100050.0)
        assert pct == pytest.approx(9.945, abs=0.01)

    def test_day_pace_retarget(self, db):
        con = _con(db)
        try:
            # 144 blocks/day is exactly target pace.
            assert warehouse.day_pace_retarget(con) == pytest.approx(0.0)
        finally:
            con.close()


class TestCollect:
    def test_missing_file_is_unavailable_not_an_exception(self, tmp_path):
        cfg = Config.from_env(db_path=tmp_path / "nope.duckdb")
        r = warehouse.collect(cfg)
        assert r.available is False
        assert "not found" in r.error

    def test_collect_shape(self, db):
        r = warehouse.collect(Config.from_env(db_path=db))
        assert r.available
        assert r.data["date"] == _utc_today().isoformat()
        assert r.data["signals"]["apathy_days"] == 0
        assert r.data["warehouse_stale"] is False
        assert r.data["onchain"]["fee_subsidy"] == 2.0

    def test_stale_when_behind(self, tmp_path):
        con = duckdb.connect(str(tmp_path / "old.duckdb"))
        con.execute(
            "CREATE TABLE onchain (date DATE PRIMARY KEY, hash_rate_ehs DOUBLE, "
            "difficulty_t DOUBLE, blocks_day INTEGER, block_fullness DOUBLE, "
            "p50_fee DOUBLE, miner_rev DOUBLE, fee_subsidy DOUBLE, tx_rate DOUBLE, "
            "retarget_proj DOUBLE)"
        )
        con.execute("CREATE TABLE btc (date DATE PRIMARY KEY, close DOUBLE, "
                    "kraken_vol DOUBLE, kraken_trades BIGINT)")
        old = _utc_today() - datetime.timedelta(days=10)
        con.execute("INSERT INTO onchain VALUES (?, 800, 100, 144, 95, 2, 450, 2.0, 6.5, 0)",
                    [old])
        con.close()

        r = warehouse.collect(Config.from_env(db_path=tmp_path / "old.duckdb"))
        assert r.available and r.stale
        assert r.data["days_behind"] == 10


class TestIdentGuard:
    @pytest.mark.parametrize("bad", ["fee_subsidy; DROP TABLE onchain", "1bad", "a b"])
    def test_rejects_unsafe_identifiers(self, bad):
        with pytest.raises(ValueError, match="unsafe SQL identifier"):
            warehouse._ident(bad)


class TestPriceComesFromTheBtcTable:
    """Regression: `close` was read off the latest `onchain` row, where the
    column does not exist, so it was always None. `sma200` was non-None, and
    the render line guarded on `sma200` while formatting `close` — which took
    out the entire ON-CHAIN block with a TypeError on the first real warehouse.
    """

    def test_close_is_populated(self, tmp_path):
        path = _build_db(tmp_path / "m.duckdb", days=400,
                         close=lambda i: 63900.0 if i == 399 else 71000.0)
        r = warehouse.collect(Config.from_env(db_path=path))
        assert r.data["close"] == 63900.0
        assert r.data["sma200"] is not None

    def test_renders_with_price_present(self, tmp_path):
        path = _build_db(tmp_path / "m.duckdb", days=400)
        r = warehouse.collect(Config.from_env(db_path=path))
        assert any("daily close" in line for line in warehouse.render_lines(r.data))

    def test_renders_when_the_price_table_is_empty(self, tmp_path):
        """On-chain and price advance independently; one empty must not cost
        the block."""
        path = _build_db(tmp_path / "m.duckdb", days=400)
        con = duckdb.connect(str(path))
        con.execute("DELETE FROM btc")
        con.close()

        r = warehouse.collect(Config.from_env(db_path=path))
        assert r.available
        assert r.data["close"] is None and r.data["sma200"] is None
        lines = warehouse.render_lines(r.data)          # must not raise
        assert any("blks" in line for line in lines)
        assert not any("daily close" in line for line in lines)


class TestRealizedVolatility:
    def _walk(self, n, calm_from=None, sd=0.04, calm_sd=0.005, seed=7):
        import math, random
        random.seed(seed)
        px = [50000.0]
        for i in range(n - 1):
            s = calm_sd if (calm_from is not None and i >= calm_from) else sd
            px.append(px[-1] * math.exp(random.gauss(0, s)))
        return px

    def test_flat_prices_have_zero_volatility(self, tmp_path):
        path = _build_db(tmp_path / "flat.duckdb", days=400, close=lambda i: 70000.0)
        con = _con(path)
        try:
            assert warehouse.realized_vol(con, 30)["value"] == pytest.approx(0.0)
        finally:
            con.close()

    def test_matches_a_hand_computed_figure(self, tmp_path):
        """Pin the annualisation: sd of log returns times sqrt(365), as a %."""
        import math, statistics as st
        px = self._walk(400)
        path = _build_db(tmp_path / "w.duckdb", days=400, close=lambda i: px[i])
        lr = [math.log(px[i] / px[i - 1]) for i in range(1, len(px))]
        expected = st.stdev(lr[-30:]) * math.sqrt(365) * 100

        con = _con(path)
        try:
            assert warehouse.realized_vol(con, 30)["value"] == pytest.approx(
                expected, abs=0.05
            )
        finally:
            con.close()

    def test_a_252_day_year_would_read_lower(self):
        """Why the convention is labelled: same series, ~17% lower reading."""
        import math
        assert math.sqrt(252) / math.sqrt(365) == pytest.approx(0.831, abs=0.001)

    def test_calm_recent_stretch_ranks_at_a_low_percentile(self, tmp_path):
        px = self._walk(800, calm_from=650)
        path = _build_db(tmp_path / "calm.duckdb", days=800, close=lambda i: px[i])
        con = _con(path)
        try:
            v30 = warehouse.realized_vol(con, 30)
        finally:
            con.close()
        assert v30["covered"] and v30["percentile_recent"] < 20

    def test_compression_shows_in_the_term_structure(self, tmp_path):
        """Short below long is the compression setup the windows exist for."""
        px = self._walk(800, calm_from=650)
        path = _build_db(tmp_path / "term.duckdb", days=800, close=lambda i: px[i])
        con = _con(path)
        try:
            short = warehouse.realized_vol(con, 30)["value"]
            long_ = warehouse.realized_vol(con, 360)["value"]
        finally:
            con.close()
        assert short < long_

    def test_uncovered_window_reports_na_not_a_short_estimate(self, tmp_path):
        path = _build_db(tmp_path / "short.duckdb", days=100)
        con = _con(path)
        try:
            v = warehouse.realized_vol(con, 360)
        finally:
            con.close()
        assert v["covered"] is False
        assert v["value"] is None
        assert v["percentile_recent"] is None and v["percentile_all"] is None

    def test_percentile_needs_a_minimum_history(self, tmp_path):
        """A level can be computed long before its rank means anything."""
        path = _build_db(tmp_path / "thin.duckdb", days=40)
        con = _con(path)
        try:
            v = warehouse.realized_vol(con, 30)
        finally:
            con.close()
        assert v["covered"] and v["value"] is not None
        assert v["percentile_recent"] is None and v["percentile_all"] is None

    def test_collect_and_render(self, tmp_path):
        px = self._walk(800, calm_from=650)
        path = _build_db(tmp_path / "c.duckdb", days=800, close=lambda i: px[i])
        r = warehouse.collect(Config.from_env(db_path=path, cache_dir=tmp_path / "cc"))

        assert r.data["volatility"]["annualisation_days"] == 365
        assert [w["days"] for w in r.data["volatility"]["windows"]] == [7, 30, 90, 180, 360]

        line = next(l for l in warehouse.render_lines(r.data) if l.startswith("vol "))
        assert "ann √365" in line and "30d" in line

        ctx = " ".join(warehouse.context_lines(r.data))
        assert "SIZE of moves, not their direction" in ctx
        assert "not a bottom signal" in ctx
        assert "close-to-close" in ctx


class TestVolatilityPercentileWindows:
    """Ranked against two histories, because they disagree materially.

    Bitcoin's volatility fell as the market matured, so ranking today against
    2014-17 partly measures that decline. On the real series the 360d reading
    is 5th percentile of all history and 24th of the last two years.
    """

    def _regime_shift(self, tmp_path, name):
        """Old era genuinely wild, recent era merely quiet."""
        import math, random
        random.seed(11)
        px = [50000.0]
        for i in range(1099):
            sd = 0.012 if i >= 730 else 0.05      # calm recent 2y, wild before
            px.append(px[-1] * math.exp(random.gauss(0, sd)))
        return _build_db(tmp_path / name, days=1100, close=lambda i: px[i])

    def test_recent_window_ranks_higher_than_all_history(self, tmp_path):
        path = self._regime_shift(tmp_path, "shift.duckdb")
        con = _con(path)
        try:
            v = warehouse.realized_vol(con, 90)
        finally:
            con.close()
        assert v["percentile_all"] < v["percentile_recent"], (
            "against a wilder past the reading must look more extreme than it "
            "does against the recent regime"
        )

    def test_window_length_is_reported(self, tmp_path):
        path = self._regime_shift(tmp_path, "w.duckdb")
        con = _con(path)
        try:
            v = warehouse.realized_vol(con, 30)
        finally:
            con.close()
        assert v["percentile_window_days"] == warehouse.VOL_PERCENTILE_RECENT_DAYS

    def test_render_labels_both_windows(self, tmp_path):
        path = self._regime_shift(tmp_path, "r.duckdb")
        r = warehouse.collect(Config.from_env(db_path=path, cache_dir=tmp_path / "c"))
        line = next(l for l in warehouse.render_lines(r.data) if l.startswith("vol "))
        assert "pctile 2y/all" in line, "an unlabelled percentile is ambiguous"
        assert "ann √365" in line

    def test_analyst_is_steered_to_the_recent_window(self, tmp_path):
        path = self._regime_shift(tmp_path, "a.duckdb")
        r = warehouse.collect(Config.from_env(db_path=path, cache_dir=tmp_path / "c2"))
        ctx = " ".join(warehouse.context_lines(r.data))
        assert "Prefer the 2-year percentile" in ctx
        assert "declined structurally" in ctx
