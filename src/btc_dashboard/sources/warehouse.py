"""Deep history and signals, read from the DuckDB warehouse.

Read-only, always. A separate ingester owns writes to this file; DuckDB allows
one writer per file, and opening it read-only is what keeps this tool from ever
contending with that. Nothing here writes, and nothing here should.

The warehouse holds raw daily facts (`onchain` since 2016, `btc` bars since
2013). Everything this module reports beyond the latest row is derived at query
time, so a definition change is a code change rather than a backfill.

Two measurement decisions carry most of the weight:

**Weekly seasonality is real and large.** `fee_subsidy` runs materially lower at
weekends. A raw daily percentile over it therefore substantially reports the day
of the week — the bottom decile fills up with Saturdays and Sundays. Two
different corrections are used, and they are not interchangeable: a 7-day
trailing mean (`smooth_days=7`) spans exactly one of each weekday and cancels
the cycle, but it is a low-pass filter that blurs single-day events; ranking the
residual against the same weekday's mean (`detrend_dow`) removes the cycle while
keeping daily resolution. Sustained regimes get smoothing; transient spikes get
detrending.

**Relative and absolute thresholds answer different questions.** A percentile
recalibrates to the window it measures, so by construction only N% of days can
sit below the Nth percentile however depressed the regime — it detects a *new
low*, and can never express the *duration* of a sustained one. The apathy streak
uses an absolute threshold precisely so it can say "N days in the basement".
"""
from __future__ import annotations

import datetime
import math
import re
from pathlib import Path

from . import Metric, Panel, SourceResult, fmt, unavailable

NAME = "warehouse"

FEE_WINDOW_DAYS = 730
VOL_WINDOW_DAYS = 730
HASHRATE_WINDOW_DAYS = 90
SMA_WINDOWS = (20, 50, 200)
SMA_WINDOW = SMA_WINDOWS[-1]

# Realised volatility windows, short to long. The long end gives the term
# structure: short sitting below long is compression.
VOL_WINDOWS = (7, 30, 90, 180, 360)
# Bitcoin trades every day, so a year is 365 periods. Using 252 (an equities
# convention) understates annualised vol by ~17% — enough to move a reading
# across a published threshold, which is exactly how two correct calculations
# come to disagree. The figure is labelled in the output for that reason.
VOL_ANNUALISATION = 365
# Volatility is ranked twice. Against all history it looks more extreme than it
# is, because Bitcoin's volatility has structurally declined — median 30d vol
# ran 79% in 2014 and 38% in 2026 — so ranking today against the 2014-17 era
# partly measures the market's maturation rather than current conditions. The
# gap is largest at the long windows: 360d vol reads 5th percentile against all
# history and 24th against two years. The recent window matches the 2y used by
# the fee/subsidy signal, so the two lines mean the same thing by "percentile".
VOL_PERCENTILE_RECENT_DAYS = 730
APATHY_MAX = 1.0
# A percentile threshold fires (100-N)% of days by construction, so 95 means
# roughly 18 days a year rather than ~36 at 90 — rare enough that surfacing it
# still means something.
VOL_PCTILE_MIN = 95.0
MIN_WINDOW_ROWS = 30
# Days behind the last COMPLETE UTC day before the panel says so. Not days
# behind *today*: the warehouse only ever stores finished days, so it is
# structurally at least one day behind today and measuring against today
# inflates every reading by one.
#
# 1 rather than 0 because there is a legitimate window each day — between a UTC
# day completing at 00:00 and the ingester collecting it — when being one day
# behind is correct. Warning at 0 would fire daily for that whole window; this
# fires only once a scheduled run has actually been missed.
STALE_AFTER_DAYS = 1

# Blocks per day at the 10-minute target.
BLOCKS_PER_DAY = 144
# Block discovery is Poisson, so one day's count has sd = sqrt(144) = 12
# blocks — about 8% of the target. A single day therefore says almost nothing
# about difficulty direction: a day at -12% sits ~1.4sd low, which happens
# roughly one day in twelve by chance alone. The band is rendered next to the
# figure so it is read as one day's outcome rather than as a projection; the
# node's cumulative estimate, computed over the whole difficulty period, is
# the number to trust for direction.
PACE_NOISE_PCT = 100 / math.sqrt(BLOCKS_PER_DAY)

# The ingester appends one row per UTC day, so the underlying data changes at
# most daily — but each collection runs several full-table scans (two 730-day
# percentiles, a 90-day drawdown, a 200-row SMA, and a streak walk over every
# row). An hour's cache removes that work from the common path entirely.
#
# Note this caches the *derived* view, not the database: `--refresh` re-reads,
# and the file is still opened read-only, so a running ingester is unaffected.
CACHE_TTL = 3600

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str) -> str:
    """Guard the identifiers that get interpolated into SQL.

    Column and table names here are module constants, not user input, but they
    reach SQL via f-strings (DuckDB won't parameterize identifiers). This keeps
    a future caller-supplied column from becoming an injection point.
    """
    if not _IDENT.match(name):
        raise ValueError(f"unsafe SQL identifier: {name!r}")
    return name


def _connect(db_path: Path):
    import duckdb

    return duckdb.connect(str(db_path), read_only=True)


def _scalar(con, sql: str):
    row = con.execute(sql).fetchone()
    return row[0] if row else None


def latest_row(con, table: str) -> dict | None:
    rel = con.execute(f"SELECT * FROM {_ident(table)} ORDER BY date DESC LIMIT 1")
    cols = [d[0] for d in rel.description]
    row = rel.fetchone()
    return dict(zip(cols, row)) if row else None


def percentile_rank(
    con, column: str, table: str = "onchain", window_days: int = 730,
    smooth_days: int = 1, detrend_dow: bool = False,
) -> float | None:
    """Percentile (0-100) of the latest value within its trailing window."""
    col, tbl = _ident(column), _ident(table)
    if detrend_dow:
        series = f"""
            base AS (
                SELECT date, {col} AS v, isodow(date) AS dw FROM {tbl}
                WHERE {col} IS NOT NULL
                  AND date > (SELECT max(date) FROM {tbl}) - INTERVAL '{window_days}' DAY
            ),
            dm AS (SELECT dw, avg(v) AS m FROM base GROUP BY dw),
            w AS (SELECT b.date, b.v - dm.m AS v FROM base b JOIN dm ON b.dw = dm.dw)
        """
    else:
        series = f"""
            s AS (
                SELECT date, avg({col}) OVER (
                    ORDER BY date
                    RANGE BETWEEN INTERVAL '{max(0, smooth_days - 1)}' DAY PRECEDING
                              AND CURRENT ROW
                ) AS v
                FROM {tbl} WHERE {col} IS NOT NULL
            ),
            w AS (
                SELECT date, v FROM s
                WHERE date > (SELECT max(date) FROM {tbl}) - INTERVAL '{window_days}' DAY
            )
        """
    # Ties are counted explicitly and split, rather than compared with a bare
    # `<`. Two reasons, and the first is not theoretical: the sliding-window
    # avg() is evaluated incrementally, so values that are mathematically equal
    # differ in their last floating-point bits and a strict `<` splits them
    # arbitrarily — a perfectly flat series was ranking at the 58th percentile
    # instead of the middle. EPS absorbs that. Splitting the ties then gives the
    # standard mid-rank, so a value equal to everything else reads 50th rather
    # than 0th or 100th depending on which way the noise fell.
    eps = "1e-9 * greatest(abs(latest.v), 1)"
    row = con.execute(
        f"""
        WITH {series}, latest AS (SELECT v FROM w ORDER BY date DESC LIMIT 1)
        SELECT (SELECT count(*) FROM w),
               (SELECT count(*) FROM w, latest WHERE w.v < latest.v - {eps}),
               (SELECT count(*) FROM w, latest WHERE abs(w.v - latest.v) <= {eps})
        """
    ).fetchone()
    if not row:
        return None
    n, below, ties = row
    if not n or n < MIN_WINDOW_ROWS:
        return None
    return 100.0 * (below + ties / 2.0) / n


def drawdown_from_high(
    con, column: str, table: str = "onchain", window_days: int = 90
) -> float | None:
    """Latest value as signed % of the trailing-window max — 0 at a new high.

    Steadier than a point-to-point 7d delta for miner stress, because a single
    noisy day can't swing it.
    """
    col, tbl = _ident(column), _ident(table)
    row = con.execute(
        f"""
        WITH w AS (
            SELECT date, {col} AS v FROM {tbl}
            WHERE {col} IS NOT NULL
              AND date > (SELECT max(date) FROM {tbl}) - INTERVAL '{window_days}' DAY
        )
        SELECT (SELECT count(*) FROM w),
               (SELECT v FROM w ORDER BY date DESC LIMIT 1),
               (SELECT max(v) FROM w)
        """
    ).fetchone()
    if not row:
        return None
    n, latest, mx = row
    if not n or n < MIN_WINDOW_ROWS or latest is None or not mx:
        return None
    return (latest - mx) / mx * 100


def apathy_streak(con, threshold: float = APATHY_MAX) -> int:
    """Consecutive days below an absolute fee/subsidy threshold."""
    rows = con.execute("SELECT fee_subsidy FROM onchain ORDER BY date DESC").fetchall()
    n = 0
    for (v,) in rows:
        if v is None or v >= threshold:
            break
        n += 1
    return n


def moving_average(con, days: int) -> tuple[float | None, float | None]:
    """SMA over the last `days` closes, and the latest close's % distance.

    Unlike the price source this includes the latest row: warehouse bars are
    completed daily closes, not an in-progress candle.

    Returns `(None, None)` when fewer than `days` closes exist — a short window
    must never be averaged and labelled with the longer period.
    """
    n_days = int(days)
    row = con.execute(
        f"""
        WITH recent AS (
            SELECT close FROM btc WHERE close IS NOT NULL
            ORDER BY date DESC LIMIT {n_days}
        )
        SELECT (SELECT count(*) FROM recent), (SELECT avg(close) FROM recent),
               (SELECT close FROM btc WHERE close IS NOT NULL ORDER BY date DESC LIMIT 1)
        """
    ).fetchone()
    if not row:
        return None, None
    n, sma, latest = row
    if n < n_days or not sma or latest is None:
        return None, None
    return sma, (latest - sma) / sma * 100


def sma200(con) -> tuple[float | None, float | None]:
    return moving_average(con, SMA_WINDOW)


def realized_vol(con, days: int) -> dict:
    """Close-to-close realised volatility over `days`, and its percentile.

    Returns the annualised level *and* where it sits in the full history,
    because the two answer different questions and only one of them travels.
    An absolute level depends on the annualisation convention, the price
    source, and the close time; a percentile does not. Comparing a level
    against someone else's published threshold silently compares conventions
    as much as markets.

    Close-only by necessity: the warehouse has no OHLC, so the range-based
    estimators (Parkinson, Garman-Klass) that are several times more efficient
    per observation are unavailable. This is a noisier estimate than one built
    from intraday highs and lows.
    """
    w = int(days)
    eps = "1e-9 * greatest(abs(latest.vol), 1)"
    row = con.execute(
        f"""
        WITH r AS (
            SELECT date, ln(close / lag(close) OVER (ORDER BY date)) AS lr
            FROM btc WHERE close IS NOT NULL
        ),
        s AS (SELECT date, lr FROM r WHERE lr IS NOT NULL),
        v AS (
            SELECT date,
                   stddev_samp(lr) OVER (
                       ORDER BY date ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW
                   ) * sqrt({VOL_ANNUALISATION}) * 100 AS vol,
                   count(lr) OVER (
                       ORDER BY date ROWS BETWEEN {w - 1} PRECEDING AND CURRENT ROW
                   ) AS n
            FROM s
        ),
        fw AS (SELECT date, vol FROM v WHERE n >= {w} AND vol IS NOT NULL),
        latest AS (SELECT vol FROM fw ORDER BY date DESC LIMIT 1),
        recent AS (
            SELECT vol FROM fw
            WHERE date > (SELECT max(date) FROM fw)
                        - INTERVAL '{VOL_PERCENTILE_RECENT_DAYS}' DAY
        )
        SELECT (SELECT count(*) FROM fw),
               (SELECT vol FROM latest),
               (SELECT count(*) FROM fw, latest WHERE fw.vol < latest.vol - {eps}),
               (SELECT count(*) FROM fw, latest WHERE abs(fw.vol - latest.vol) <= {eps}),
               (SELECT count(*) FROM recent),
               (SELECT count(*) FROM recent, latest WHERE recent.vol < latest.vol - {eps}),
               (SELECT count(*) FROM recent, latest
                WHERE abs(recent.vol - latest.vol) <= {eps})
        """
    ).fetchone()

    out = {"days": w, "covered": False, "value": None,
           "percentile_recent": None, "percentile_all": None,
           "percentile_window_days": VOL_PERCENTILE_RECENT_DAYS,
           "days_available": 0}
    if not row:
        return out
    n_all, value, below_all, ties_all, n_rec, below_rec, ties_rec = row
    out["days_available"] = n_all or 0
    if not n_all or value is None:
        return out
    out["covered"] = True
    out["value"] = round(value, 1)

    # Mid-ranked, like the other percentiles here, so a value tied with its
    # neighbours reads as the middle rather than as an extreme.
    def rank(n, below, ties):
        if not n or n < MIN_WINDOW_ROWS:
            return None
        return round(100.0 * (below + ties / 2.0) / n, 1)

    out["percentile_all"] = rank(n_all, below_all, ties_all)
    out["percentile_recent"] = rank(n_rec, below_rec, ties_rec)
    return out


def latest_close(con) -> float | None:
    """Most recent daily close from the `btc` table.

    Deliberately a separate query from the `onchain` row: price and on-chain
    facts live in different tables and advance independently, so reading price
    off the on-chain row silently yields None.
    """
    return _scalar(
        con, "SELECT close FROM btc WHERE close IS NOT NULL ORDER BY date DESC LIMIT 1"
    )


def day_pace_retarget(con) -> float | None:
    """Difficulty adjustment implied by the last complete day's block count."""
    v = _scalar(
        con,
        "SELECT blocks_day FROM onchain WHERE blocks_day IS NOT NULL "
        "ORDER BY date DESC LIMIT 1",
    )
    return (v / 144.0 - 1) * 100 if v is not None else None


def _try(fn, *a, **kw):
    """Each signal is independently fail-soft — one failing must not cost the rest."""
    try:
        return fn(*a, **kw)
    except Exception:
        return None


def collect(cfg) -> SourceResult:
    db = Path(cfg.db_path)
    if not db.exists():
        return unavailable(NAME, f"warehouse not found at {db}")
    try:
        con = _connect(db)
    except Exception as e:
        return unavailable(NAME, f"cannot open {db} read-only: {e}")

    try:
        row = _try(latest_row, con, "onchain")
        if not row or row.get("date") is None:
            return unavailable(NAME, "no usable row in onchain")

        date = row["date"]
        smas = []
        for days in SMA_WINDOWS:
            value, pct = _try(moving_average, con, days) or (None, None)
            smas.append({
                "days": days,
                "covered": value is not None,
                "value": round(value, 2) if value is not None else None,
                "pct": round(pct, 2) if pct is not None else None,
            })
        primary = next(s for s in smas if s["days"] == SMA_WINDOW)
        sma, sma_pct = primary["value"], primary["pct"]

        vol = {
            "annualisation_days": VOL_ANNUALISATION,
            "windows": [
                _try(realized_vol, con, w)
                or {"days": w, "covered": False, "value": None,
                    "percentile": None, "days_available": 0}
                for w in VOL_WINDOWS
            ],
        }
        data = {
            "date": date.isoformat(),
            "onchain": {
                k: row.get(k)
                for k in (
                    "blocks_day", "block_fullness", "p50_fee", "fee_subsidy",
                    "miner_rev", "hash_rate_ehs", "difficulty_t", "tx_rate",
                )
            },
            "signals": {
                # Smoothed, not detrended: a sustained fee regime is exactly what
                # a 7-day mean should preserve.
                "fee_pctile": _try(
                    percentile_rank, con, "fee_subsidy",
                    window_days=FEE_WINDOW_DAYS, smooth_days=7,
                ),
                "apathy_days": _try(apathy_streak, con),
                "hashrate_drawdown": _try(
                    drawdown_from_high, con, "hash_rate_ehs",
                    window_days=HASHRATE_WINDOW_DAYS,
                ),
                # Detrended, not smoothed: a volume event lasts days, and a
                # 7-day mean dilutes it to nothing.
                "vol_pctile": _try(
                    percentile_rank, con, "kraken_vol", table="btc",
                    window_days=VOL_WINDOW_DAYS, detrend_dow=True,
                ),
            },
            "close": _try(latest_close, con),
            "smas": smas,
            "volatility": vol,
            # Flat aliases for the primary window, kept for schema continuity.
            "sma200": sma,
            "sma200_pct": sma_pct,
            "day_pace_retarget": _try(day_pace_retarget, con),
        }

        behind = days_behind(date)
        data["days_behind"] = behind
        data["warehouse_stale"] = behind > STALE_AFTER_DAYS

        return SourceResult(
            name=NAME, available=True, data=data,
            as_of=date.isoformat(), stale=data["warehouse_stale"],
        )
    finally:
        con.close()


def days_behind(date: datetime.date, now: datetime.datetime | None = None) -> int:
    """How many complete UTC days the warehouse is missing. 0 = fully current.

    Measured against the last *complete* day rather than today, because today
    is never in the warehouse by construction — only finished days are stored.
    Measuring against today reported a healthy warehouse as "2d behind" while
    it was missing exactly one day.
    """
    now = now or datetime.datetime.now(datetime.timezone.utc)
    last_complete = now.date() - datetime.timedelta(days=1)
    return max(0, (last_complete - date).days)


def refresh_derived(data: dict) -> dict:
    """Recompute how far behind the warehouse is, against the clock now.

    `days_behind` and `warehouse_stale` are relative to the current date, so
    they age with the cache. Recomputed on every cache read, in UTC — the
    warehouse buckets by UTC calendar day.
    """
    try:
        date = datetime.date.fromisoformat(data["date"])
    except (KeyError, TypeError, ValueError):
        return data
    behind = days_behind(date)
    data["days_behind"] = behind
    data["warehouse_stale"] = behind > STALE_AFTER_DAYS
    return data


def _pctile(value) -> str:
    """Percentile for the panel, without letting rounding overstate an extreme.

    A mid-ranked percentile can never actually reach 0 or 100: the single
    lowest of 730 observations ranks 0.07, not 0. Rounding to an integer
    therefore prints "0" for anything in the bottom half-percent, which reads
    as "the lowest ever recorded" when it may be the second-lowest of two
    years. The extremes report as a band instead.

    The rounded text is compared rather than the number because Python rounds
    halves to even, so 0.5 formats as "0" and a threshold test on the value
    alone would miss it.
    """
    if not isinstance(value, (int, float)):
        return "-"
    text = f"{value:.0f}"
    if text == "0" and value > 0:
        return "<1"
    if text == "100" and value < 100:
        return ">99"
    return text


def _ordinal(p: float) -> str:
    n = max(1, round(p))
    suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _day_label(raw) -> str:
    """`UTC <date> <weekday>`, or a plain fallback if the date is unusable.

    The weekday is load-bearing, not decoration: fee_subsidy is seasonally
    lower at weekends, so an unlabelled weekend dip reads as deterioration
    rather than as a Saturday.
    """
    try:
        date = datetime.date.fromisoformat(raw)
        return f"UTC {date} {date:%a}"
    except (TypeError, ValueError):
        return f"UTC {raw or 'unknown date'}"


def render_lines(d: dict) -> list[str]:
    oc = d.get("onchain") or {}
    sig = d.get("signals") or {}
    parts = []
    if oc.get("blocks_day") is not None:
        parts.append(f"{fmt(oc.get('blocks_day'))} blks")
    if oc.get("block_fullness") is not None:
        parts.append(f"{fmt(oc.get('block_fullness'), '.0f')}% full")
    if oc.get("p50_fee") is not None:
        # getblockstats reports integer sat/vB, so 0 is a floor meaning "under
        # 1", not an absence of fees — rendering it as 0.0 reads as missing data.
        p50 = oc["p50_fee"]
        parts.append(
            "p50 <1 sat/vB" if isinstance(p50, (int, float)) and p50 < 1
            else f"p50 {fmt(p50, '.1f')} sat/vB"
        )
    if oc.get("fee_subsidy") is not None:
        parts.append(f"fee/subsidy {fmt(oc.get('fee_subsidy'), '.2f')}%")
    if oc.get("miner_rev") is not None:
        parts.append(f"miner rev {fmt(oc.get('miner_rev'), ',.1f')} BTC")

    out = [f"day ({_day_label(d.get('date'))}): " + " | ".join(parts)] if parts else []

    bits = []
    if sig.get("fee_pctile") is not None:
        bits.append(f"fee/subsidy {_ordinal(sig['fee_pctile'])} pctile 2y (7d)")
    if sig.get("apathy_days"):
        bits.append(f"apathy {fmt(sig.get('apathy_days'))}d")
    dd = sig.get("hashrate_drawdown")
    if isinstance(dd, (int, float)) and dd < -1:
        bits.append(f"hashrate {fmt(dd, '.1f')}% off 90d high")
    vol = sig.get("vol_pctile")
    if isinstance(vol, (int, float)) and vol >= VOL_PCTILE_MIN:
        bits.append(f"volume {_ordinal(vol)} pctile 2y")
    if bits:
        out.append("signal: " + " | ".join(bits))
    # Every value below is guarded independently. A partially-populated
    # warehouse is normal — the price and on-chain tables advance separately,
    # and one lagging must not cost the whole block.
    close = d.get("close")
    if close is not None:
        # Labelled as the daily bar to keep it distinct from the live spot in
        # the PRICE block, which comes from a different source and time.
        out.append(f"daily close {fmt(close, ',.0f', prefix='$')} (warehouse)")

    parts = []
    for s in d.get("smas") or []:
        if not isinstance(s, dict):
            continue
        if s.get("covered"):
            parts.append(
                f"{fmt(s.get('days'))}d {fmt(s.get('value'), ',.0f', prefix='$')} "
                f"{fmt(s.get('pct'), '+.1f', suffix='%')}"
            )
        else:
            parts.append(f"{fmt(s.get('days'))}d n/a")
    if parts:
        out.append("SMA " + " | ".join(parts))

    vol = d.get("volatility") or {}
    vparts = []
    for w in vol.get("windows") or []:
        if not isinstance(w, dict):
            continue
        if not w.get("covered"):
            vparts.append(f"{fmt(w.get('days'))}d n/a")
            continue
        rec, all_ = w.get("percentile_recent"), w.get("percentile_all")
        tail = ""
        if rec is not None or all_ is not None:
            tail = f" ({_pctile(rec)}/{_pctile(all_)})"
        vparts.append(f"{fmt(w.get('days'))}d {fmt(w.get('value'), '.0f')}%{tail}")
    if vparts:
        # The annualisation is named because the level is meaningless without
        # it: the same series on a 252-day convention reads ~17% lower, which
        # is enough to put a reading the wrong side of a published threshold.
        # Both percentile windows are shown because they diverge by up to 19
        # points at the long end, where the all-history figure is substantially
        # reporting Bitcoin's declining volatility rather than today.
        years = fmt(vol.get("percentile_window_days", 730) // 365)
        out.append(
            f"vol (ann √{fmt(vol.get('annualisation_days'))}, pctile {years}y/all): "
            + " | ".join(vparts)
        )

    # Described as that day's block pace, not as a "retarget projection". The
    # node block already carries a cumulative projection computed over the full
    # difficulty period; showing a second, far noisier number under the same
    # name invites reading the unreliable one as the trend.
    pace, blocks = d.get("day_pace_retarget"), oc.get("blocks_day")
    if pace is not None and blocks is not None:
        out.append(
            f"block pace {fmt(blocks)}/{BLOCKS_PER_DAY} "
            f"({fmt(pace, '+.1f')}%, ±{PACE_NOISE_PCT:.0f}% day-to-day noise)"
        )
    elif pace is not None:
        out.append(
            f"block pace {fmt(pace, '+.1f')}% vs target "
            f"(one day, ±{PACE_NOISE_PCT:.0f}% noise)"
        )

    if d.get("warehouse_stale"):
        out.append(
            f"warehouse {fmt(d.get('days_behind'), missing='?')}d behind the last "
            f"complete UTC day — the ingester has missed a run"
        )
    return out


def context_lines(d: dict) -> list[str]:
    """History the analyst cannot otherwise see.

    Without these it has only today's numbers and no way to tell an ordinary
    reading from an extreme one, which is exactly when it starts inventing
    thresholds.
    """
    sig = d.get("signals") or {}
    out: list[str] = []
    if sig.get("fee_pctile") is not None:
        out.append(
            f"BTC fee/subsidy vs history: {fmt(sig.get('fee_pctile'), '.0f')}th "
            f"percentile of 2y (7d mean, weekend-corrected; low = blockspace "
            f"demand apathy)"
        )
    if sig.get("apathy_days"):
        out.append(
            f"BTC apathy streak: {fmt(sig.get('apathy_days'))} consecutive days under "
            f"{APATHY_MAX}% fee/subsidy. This is regime DURATION against a fixed "
            f"threshold, not a new low."
        )
    dd = sig.get("hashrate_drawdown")
    if isinstance(dd, (int, float)) and dd < -1:
        out.append(
            f"BTC hashrate: {fmt(dd, '.1f')}% off its 90d high (miner stress; historic "
            f"washouts ran -33% to -50%)"
        )
    vol = sig.get("vol_pctile")
    if isinstance(vol, (int, float)) and vol >= VOL_PCTILE_MIN:
        out.append(
            f"BTC exchange volume: {fmt(vol, '.0f')}th percentile of 2y (single venue, "
            f"weekday-adjusted). Volume marks EVENTS, not direction — pair it with "
            f"the price move. It fires on acute capitulation, stays quiet at slow "
            f"bear bottoms and at every euphoria peak, so it flags the panic itself, "
            f"not the low, which typically follows days later."
        )
    vol = d.get("volatility") or {}
    covered = [w for w in (vol.get("windows") or [])
               if isinstance(w, dict) and w.get("covered")]
    if covered:
        years = fmt(vol.get("percentile_window_days", 730) // 365)
        levels = ", ".join(
            f"{fmt(w.get('days'))}d {fmt(w.get('value'), '.0f')}%"
            + (f" ({_ordinal(w['percentile_recent'])} pctile of the last {years}y"
               + (f", {_ordinal(w['percentile_all'])} of all history)"
                  if w.get("percentile_all") is not None else ")")
               if w.get("percentile_recent") is not None else "")
            for w in covered
        )
        out.append(
            f"BTC realised volatility, annualised on a "
            f"{fmt(vol.get('annualisation_days'))}-day year: {levels}."
        )
        out.append(
            f"Prefer the {years}-year percentile. Bitcoin's volatility has "
            f"declined structurally as the market matured — median 30d vol ran "
            f"about 79% in 2014 against 38% in 2026 — so the all-history figure "
            f"partly reports that decline rather than current conditions, and "
            f"reads more extreme than the recent one, especially at the long "
            f"windows."
        )
        out.append(
            "Volatility describes the SIZE of moves, not their direction. A low "
            "reading says the next move is more likely to be large than that it "
            "will be upward, and it is not a bottom signal. Historically both "
            "very low and very high readings preceded larger moves than mid-range "
            "ones, while the sign of those moves did not separate."
        )
        out.append(
            "These are close-to-close estimates from daily bars — no intraday "
            "high/low, so they are noisier than range-based estimators, and the "
            "level depends on the annualisation convention. Compare the "
            "percentile rather than the level against any externally published "
            "volatility threshold."
        )

    if out:
        out.append(
            f"The on-chain day above is {_day_label(d.get('date'))}, a complete day. "
            f"Fee/subsidy runs materially lower at weekends — the percentiles above "
            f"already correct for that, the raw daily figures do not."
        )
    if d.get("warehouse_stale"):
        out.append(
            f"WARNING: the warehouse is missing "
            f"{fmt(d.get('days_behind'), missing='an unknown number of')} complete "
            f"UTC day(s); the on-chain figures are not current."
        )
    return out


def html_panels(d: dict) -> list[Panel]:
    oc = d.get("onchain") or {}
    sig = d.get("signals") or {}
    vol = d.get("volatility") or {}
    years = fmt(vol.get("percentile_window_days", 730) // 365)

    pace, blocks = d.get("day_pace_retarget"), oc.get("blocks_day")
    facts = [
        Metric("Blocks", fmt(blocks),
               note=(f"{fmt(pace, '+.1f')}% vs {BLOCKS_PER_DAY} target, "
                     f"±{PACE_NOISE_PCT:.0f}% day-to-day noise"
                     if pace is not None else None)),
        Metric("Block Fullness", f"{fmt(oc.get('block_fullness'), '.0f')}%"),
        Metric("Median Fee",
               "<1 sat/vB" if isinstance(oc.get("p50_fee"), (int, float))
               and oc["p50_fee"] < 1 else f"{fmt(oc.get('p50_fee'), '.1f')} sat/vB",
               note="integer sat/vB from getblockstats — 0 means under 1"),
        Metric("Fee / Subsidy", f"{fmt(oc.get('fee_subsidy'), '.2f')}%"),
        Metric("Miner Revenue", f"{fmt(oc.get('miner_rev'), ',.1f')} BTC"),
        Metric("Daily Close", fmt(d.get("close"), ",.0f", prefix="$"),
               note=f"settled bar for {_day_label(d.get('date')).replace('UTC ', '')}"
                    f", not live spot"),
    ]

    signals = []
    if sig.get("fee_pctile") is not None:
        signals.append(Metric(
            "Fee / Subsidy", f"{_ordinal(sig['fee_pctile'])} pctile",
            note=f"{years}y · 7d mean, weekend-corrected · low = demand apathy"))
    if sig.get("apathy_days"):
        signals.append(Metric(
            "Apathy Streak", f"{fmt(sig.get('apathy_days'))}d",
            note=f"consecutive days under {APATHY_MAX}% — duration, not a new low"))
    dd = sig.get("hashrate_drawdown")
    if isinstance(dd, (int, float)):
        signals.append(Metric(
            "Hashrate Drawdown", f"{fmt(dd, '.1f')}%",
            note=f"off its {HASHRATE_WINDOW_DAYS}d high · washouts ran -33% to -50%",
            tone="warn" if dd < -20 else None))
    v = sig.get("vol_pctile")
    if isinstance(v, (int, float)) and v >= VOL_PCTILE_MIN:
        signals.append(Metric(
            "Exchange Volume", f"{_ordinal(v)} pctile",
            note=f"{years}y, single venue · marks events, not direction"))

    vols = []
    for w in vol.get("windows") or []:
        if not isinstance(w, dict):
            continue
        if not w.get("covered"):
            vols.append(Metric(f"{fmt(w.get('days'))}D", "n/a",
                               note="not enough history for this window"))
            continue
        vols.append(Metric(
            f"{fmt(w.get('days'))}D", f"{fmt(w.get('value'), '.1f')}%",
            note=f"{_pctile(w.get('percentile_recent'))} pctile {years}y · "
                 f"{_pctile(w.get('percentile_all'))} all history"))

    # The day goes in the heading, not in a row. As a row it reads as one
    # metric among many, and every other figure on the card is *for* that day —
    # so a reader comparing it against a live price has no cue that they are
    # looking at different days. The weekday stays: fee/subsidy runs materially
    # lower at weekends, so an unlabelled Saturday reads as deterioration.
    try:
        day = datetime.date.fromisoformat(d["date"])
        heading = f"ON-CHAIN \u00b7 {day:%-d %b %a} UTC".upper()
    except (KeyError, TypeError, ValueError):
        heading = "ON-CHAIN (DAILY)"

    panels = [Panel(heading, facts)]
    if signals:
        panels.append(Panel(f"SIGNALS (vs {years}y)", signals))
    if vols:
        # The annualisation lives in the title so every row inherits it — the
        # level is not comparable to anyone else's without it.
        panels.append(Panel(
            f"VOLATILITY (REALISED, ann √{fmt(vol.get('annualisation_days'))})", vols))
    return panels
