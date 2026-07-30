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
import re
from pathlib import Path

from . import SourceResult, fmt, unavailable

NAME = "warehouse"

FEE_WINDOW_DAYS = 730
VOL_WINDOW_DAYS = 730
HASHRATE_WINDOW_DAYS = 90
SMA_WINDOW = 200
APATHY_MAX = 1.0
# A percentile threshold fires (100-N)% of days by construction, so 95 means
# roughly 18 days a year rather than ~36 at 90 — rare enough that surfacing it
# still means something.
VOL_PCTILE_MIN = 95.0
MIN_WINDOW_ROWS = 30
STALE_AFTER_DAYS = 2

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


def sma200(con) -> tuple[float | None, float | None]:
    row = con.execute(
        f"""
        WITH recent AS (
            SELECT close FROM btc WHERE close IS NOT NULL
            ORDER BY date DESC LIMIT {SMA_WINDOW}
        )
        SELECT (SELECT count(*) FROM recent), (SELECT avg(close) FROM recent),
               (SELECT close FROM btc WHERE close IS NOT NULL ORDER BY date DESC LIMIT 1)
        """
    ).fetchone()
    if not row:
        return None, None
    n, sma, latest = row
    if n < SMA_WINDOW or not sma or latest is None:
        return None, None
    return sma, (latest - sma) / sma * 100


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
        sma, sma_pct = _try(sma200, con) or (None, None)
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
            "sma200": round(sma, 2) if sma else None,
            "sma200_pct": round(sma_pct, 2) if sma_pct is not None else None,
            "day_pace_retarget": _try(day_pace_retarget, con),
        }

        behind = (datetime.datetime.now(datetime.timezone.utc).date() - date).days
        data["days_behind"] = behind
        data["warehouse_stale"] = behind > STALE_AFTER_DAYS

        return SourceResult(
            name=NAME, available=True, data=data,
            as_of=date.isoformat(), stale=data["warehouse_stale"],
        )
    finally:
        con.close()


def refresh_derived(data: dict) -> dict:
    """Recompute how far behind the warehouse is, against the clock now.

    `days_behind` and `warehouse_stale` describe the gap between the newest
    stored day and today, so they age with the cache. Recomputed on every
    cache read, in UTC — the warehouse buckets by UTC calendar day.
    """
    try:
        date = datetime.date.fromisoformat(data["date"])
    except (KeyError, TypeError, ValueError):
        return data
    behind = (datetime.datetime.now(datetime.timezone.utc).date() - date).days
    data["days_behind"] = behind
    data["warehouse_stale"] = behind > STALE_AFTER_DAYS
    return data


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
    close, sma = d.get("close"), d.get("sma200")
    if close is not None and sma is not None:
        # Labelled as the daily bar to keep it distinct from the live spot in
        # the PRICE block, which comes from a different source and time.
        out.append(
            f"daily close {fmt(close, ',.0f', prefix='$')} | "
            f"200d SMA {fmt(sma, ',.0f', prefix='$')} (warehouse)"
        )
    elif close is not None:
        out.append(
            f"daily close {fmt(close, ',.0f', prefix='$')} (warehouse; 200d SMA n/a)"
        )

    if d.get("day_pace_retarget") is not None:
        out.append(f"day-pace retarget {fmt(d.get('day_pace_retarget'), '+.2f')}%")

    if d.get("warehouse_stale"):
        out.append(f"warehouse {fmt(d.get('days_behind'), missing='?')}d behind")
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
    if out:
        out.append(
            f"The on-chain day above is {_day_label(d.get('date'))}, a complete day. "
            f"Fee/subsidy runs materially lower at weekends — the percentiles above "
            f"already correct for that, the raw daily figures do not."
        )
    if d.get("warehouse_stale"):
        out.append(
            f"WARNING: warehouse is {fmt(d.get('days_behind'), missing='an unknown number of')} "
            f"days behind; on-chain figures are not current."
        )
    return out
