#!/usr/bin/env python3
"""Do hashrate-derived indicators mark cycle price bottoms?

Runs three things against the warehouse and prints them side by side:

1. **Drawdown episodes**, derived from the price series rather than hardcoded,
   so the study stays correct as new data arrives and does not encode a set of
   dates someone chose after the fact.
2. **The hash ribbon** (fast MA of hashrate crossing back above the slow MA),
   scored against the *unconditional base rate* at several horizons, plus its
   timing relative to each derived trough.
3. **Forward returns conditioned on hashrate drawdown**, over every day rather
   than over the handful of troughs — the prospective form of the question,
   which does not require knowing in advance which lows turned out to matter.

Why the base-rate column exists
-------------------------------
BTC's unconditional 365-day median return over this sample is strongly
positive. Any signal evaluated on its own absolute returns therefore looks
excellent. A signal is only interesting where it beats the return of entering
on a random day, which is what the `base` column measures.

Why the effective-sample line exists
------------------------------------
Daily rows are not independent observations. Hashrate drawdown is heavily
autocorrelated and the forward windows overlap almost completely, so a decile
holding 386 days contains barely more than one independent 365-day window. The
report prints that number next to the sample count, because a table of medians
over 386 rows otherwise implies a precision the data cannot support.

Usage
-----
    python tools/hashrate_study.py
    python tools/hashrate_study.py --db ~/data/market.duckdb
    python tools/hashrate_study.py --json
    python tools/hashrate_study.py --min-depth 50 --horizons 30,90,180

Read-only: opens the warehouse with `read_only=True` and never writes.
"""
from __future__ import annotations

import argparse
import datetime
import json
import statistics as st
import sys
from dataclasses import asdict, dataclass, field

FAST_MA = 30
SLOW_MA = 60
DRAWDOWN_WINDOW = 90
DEFAULT_HORIZONS = (90, 180, 365)
DEFAULT_MIN_DEPTH = 25.0
# An episode ends when price recovers to within this distance of its old peak.
# Without a recovery band, a series oscillating either side of the entry
# threshold would be counted as many separate episodes.
RECOVERY_PCT = -10.0
DECILES = 10
# How far from a trough a ribbon signal may sit and still be called "its"
# signal. Without a bound, "nearest signal" always finds one — the 2017
# corrections matched signals 200-390 days away, which reports a relationship
# where there is only a long gap.
MAX_ASSOCIATION_DAYS = 90

ETF_LAUNCH = datetime.date(2024, 1, 11)


# ── pure computation ────────────────────────────────────────────────────────

def moving_average(xs: list[float], n: int) -> list[float | None]:
    """Trailing mean, `None` until the window is full.

    Padding with None rather than a partial mean matters: a partial window is
    a different statistic, and letting it through would put a 3-day mean and a
    60-day mean on the same axis.
    """
    out: list[float | None] = []
    total = 0.0
    for i, x in enumerate(xs):
        total += x
        if i >= n:
            total -= xs[i - n]
        out.append(total / n if i >= n - 1 else None)
    return out


def drawdown_from_high(xs: list[float], window: int) -> list[float]:
    """Each point as a signed % of its trailing-window maximum (0 at a high)."""
    out = []
    for i in range(len(xs)):
        peak = max(xs[max(0, i - window + 1): i + 1])
        out.append((xs[i] - peak) / peak * 100 if peak else 0.0)
    return out


def ribbon_signals(hr: list[float], fast: int = FAST_MA, slow: int = SLOW_MA) -> list[int]:
    """Indices where the fast hashrate MA crosses back above the slow one.

    The crossing, not the state: capitulation is `fast < slow`, and the signal
    is its end. Returning the state instead would mark every day of a long
    capitulation as a separate event and inflate the sample.
    """
    f, s = moving_average(hr, fast), moving_average(hr, slow)
    return [
        i for i in range(1, len(hr))
        if None not in (f[i], s[i], f[i - 1], s[i - 1])
        and f[i - 1] < s[i - 1] and f[i] >= s[i]
    ]


def forward_return(close: list[float], i: int, days: int) -> float | None:
    """Percentage return from `i` to `i + days`, or None past the end."""
    j = i + days
    if j >= len(close) or not close[i]:
        return None
    return (close[j] / close[i] - 1) * 100


@dataclass
class Episode:
    start: datetime.date
    trough_date: datetime.date
    depth_pct: float
    recovered: datetime.date | None
    era: str

    @property
    def resolved(self) -> bool:
        return self.recovered is not None


def drawdown_episodes(
    dates: list[datetime.date], close: list[float],
    min_depth: float = DEFAULT_MIN_DEPTH, recovery: float = RECOVERY_PCT,
) -> list[Episode]:
    """Peak-to-trough episodes worse than `min_depth`, from the price series.

    Derived rather than hardcoded so the study does not bake in a list of dates
    picked with hindsight — the failure mode the whole exercise is about.
    """
    peak = close[0]
    current: dict | None = None
    out: list[Episode] = []
    for i, (d, c) in enumerate(zip(dates, close)):
        peak = max(peak, c)
        dd = (c - peak) / peak * 100
        if dd <= -abs(min_depth) and current is None:
            current = {"start": d, "trough": dd, "trough_date": d}
        elif current is not None:
            if dd < current["trough"]:
                current["trough"], current["trough_date"] = dd, d
            if dd > recovery:
                out.append(Episode(
                    current["start"], current["trough_date"], current["trough"], d,
                    "ETF" if current["start"] >= ETF_LAUNCH else "pre-ETF",
                ))
                current = None
    if current is not None:
        out.append(Episode(
            current["start"], current["trough_date"], current["trough"], None,
            "ETF" if current["start"] >= ETF_LAUNCH else "pre-ETF",
        ))
    return out


def percentile_of(values: list[float], value: float) -> float:
    """Share of `values` strictly below `value`, as a percentage."""
    return 100.0 * sum(1 for v in values if v < value) / len(values) if values else 0.0


@dataclass
class HorizonStats:
    horizon: int
    signal_median: float | None
    base_median: float | None
    edge_pp: float | None
    signal_hit_rate: float | None
    base_hit_rate: float | None
    n_signals: int
    effective_n: float


def score(close: list[float], signals: list[int], horizons) -> list[HorizonStats]:
    """Signal returns against the return of entering on a random day."""
    stats = []
    for h in horizons:
        sig = [r for i in signals if (r := forward_return(close, i, h)) is not None]
        base = [r for i in range(len(close)) if (r := forward_return(close, i, h)) is not None]
        if not sig or not base:
            stats.append(HorizonStats(h, None, None, None, None, None, len(sig), 0.0))
            continue
        s_med, b_med = st.median(sig), st.median(base)
        stats.append(HorizonStats(
            horizon=h,
            signal_median=s_med,
            base_median=b_med,
            edge_pp=s_med - b_med,
            signal_hit_rate=100 * sum(1 for x in sig if x > 0) / len(sig),
            base_hit_rate=100 * sum(1 for x in base if x > 0) / len(base),
            n_signals=len(sig),
            # Signals cluster, and their forward windows overlap. Spacing them
            # by the horizon is a rough count of genuinely separate bets.
            effective_n=_effective_n(signals, h),
        ))
    return stats


def _effective_n(signals: list[int], horizon: int) -> float:
    """Roughly how many non-overlapping forward windows the signals span."""
    if not signals:
        return 0.0
    kept, last = 0, None
    for i in sorted(signals):
        if last is None or i - last >= horizon:
            kept += 1
            last = i
    return float(kept)


def decile_table(metric: list[float], close: list[float], horizons, k: int = DECILES):
    """Median forward return by `metric` decile, lowest decile first."""
    order = sorted(range(len(metric)), key=lambda i: metric[i])
    rows = []
    for d in range(k):
        grp = order[d * len(order) // k: (d + 1) * len(order) // k]
        if not grp:
            continue
        row = {
            "decile": d + 1,
            "low": metric[grp[0]],
            "high": metric[grp[-1]],
            "n": len(grp),
            "returns": {},
        }
        for h in horizons:
            vals = [r for i in grp if (r := forward_return(close, i, h)) is not None]
            row["returns"][h] = st.median(vals) if vals else None
        # Consecutive days with overlapping windows are not independent bets.
        row["effective_n"] = round(len(grp) / max(horizons), 2)
        rows.append(row)
    return rows


# ── data access ─────────────────────────────────────────────────────────────

@dataclass
class Series:
    dates: list[datetime.date] = field(default_factory=list)
    hashrate: list[float] = field(default_factory=list)
    close: list[float] = field(default_factory=list)
    price_dates: list[datetime.date] = field(default_factory=list)
    price_close: list[float] = field(default_factory=list)
    gaps: int = 0


def load(db_path) -> Series:
    import duckdb

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        joined = con.execute("""
            SELECT o.date, o.hash_rate_ehs, b.close
            FROM onchain o JOIN btc b USING(date)
            WHERE o.hash_rate_ehs IS NOT NULL AND b.close IS NOT NULL
            ORDER BY o.date
        """).fetchall()
        price = con.execute("""
            SELECT date, close FROM btc WHERE close IS NOT NULL ORDER BY date
        """).fetchall()
    finally:
        con.close()

    if not joined:
        raise SystemExit("no rows joining onchain.hash_rate_ehs to btc.close")

    s = Series(
        dates=[r[0] for r in joined],
        hashrate=[r[1] for r in joined],
        close=[r[2] for r in joined],
        price_dates=[r[0] for r in price],
        price_close=[r[1] for r in price],
    )
    s.gaps = sum(1 for i in range(1, len(s.dates)) if (s.dates[i] - s.dates[i - 1]).days > 1)
    return s


def analyse(s: Series, horizons, min_depth: float) -> dict:
    episodes = drawdown_episodes(s.price_dates, s.price_close, min_depth)
    signals = ribbon_signals(s.hashrate)
    dd = drawdown_from_high(s.hashrate, DRAWDOWN_WINDOW)
    index = {d: i for i, d in enumerate(s.dates)}

    troughs = []
    for e in episodes:
        i = index.get(e.trough_date)
        if i is None:
            continue  # trough predates hashrate coverage
        nearest = min(signals, key=lambda j: abs((s.dates[j] - e.trough_date).days), default=None)
        lag = (s.dates[nearest] - e.trough_date).days if nearest is not None else None
        if lag is not None and abs(lag) > MAX_ASSOCIATION_DAYS:
            nearest, lag = None, None
        troughs.append({
            "trough_date": e.trough_date,
            "depth_pct": e.depth_pct,
            "hashrate_drawdown": dd[i],
            "percentile": percentile_of(dd, dd[i]),
            "nearest_signal": s.dates[nearest] if nearest is not None else None,
            "signal_lag_days": lag,
        })

    last = len(s.dates) - 1
    f, sl = moving_average(s.hashrate, FAST_MA), moving_average(s.hashrate, SLOW_MA)
    return {
        "coverage": {
            "price": [s.price_dates[0], s.price_dates[-1], len(s.price_dates)],
            "joined": [s.dates[0], s.dates[-1], len(s.dates)],
            "gaps": s.gaps,
        },
        "episodes": [asdict(e) for e in episodes],
        "signals": [
            {"date": s.dates[i], "close": s.close[i],
             "returns": {h: forward_return(s.close, i, h) for h in horizons}}
            for i in signals
        ],
        "scores": [asdict(x) for x in score(s.close, signals, horizons)],
        "troughs": troughs,
        "deciles": decile_table(dd, s.close, horizons),
        "current": {
            "date": s.dates[last],
            "hashrate_drawdown": dd[last],
            "percentile": percentile_of(dd, dd[last]),
            "fast_ma": f[last],
            "slow_ma": sl[last],
            "in_capitulation": bool(f[last] is not None and sl[last] is not None and f[last] < sl[last]),
            "last_signal": s.dates[signals[-1]] if signals else None,
        },
    }


# ── rendering ───────────────────────────────────────────────────────────────

def _pct(v, width=7, dp=0):
    return f"{v:>{width}.{dp}f}%" if isinstance(v, (int, float)) else f"{'n/a':>{width+1}}"


def report(a: dict, horizons) -> str:
    out: list[str] = []
    w = out.append
    cov = a["coverage"]
    w("BTC HASHRATE STUDY")
    w("=" * 72)
    w(f"  price   {cov['price'][0]} -> {cov['price'][1]}  ({cov['price'][2]:,} days)")
    w(f"  joined  {cov['joined'][0]} -> {cov['joined'][1]}  "
      f"({cov['joined'][2]:,} days, {cov['gaps']} gaps)")

    w("\nDRAWDOWN EPISODES (derived from price, not hardcoded)")
    w(f"  {'start':>12} {'trough':>12} {'depth':>8} {'recovered':>12}  era")
    for e in a["episodes"]:
        rec = e["recovered"] or "UNRESOLVED"
        w(f"  {str(e['start']):>12} {str(e['trough_date']):>12} "
          f"{e['depth_pct']:>7.1f}% {str(rec):>12}  {e['era']}")

    w(f"\nHASH RIBBON ({FAST_MA}d MA crossing back above {SLOW_MA}d) — {len(a['signals'])} signals")
    hdr = " ".join(f"{str(h) + 'd':>9}" for h in horizons)
    w(f"  {'date':>12} {'close':>10} {hdr}")
    for sig in a["signals"]:
        cells = " ".join(_pct(sig["returns"].get(h), 8) for h in horizons)
        w(f"  {str(sig['date']):>12} {sig['close']:>10,.0f} {cells}")

    w("\n  vs the return of entering on a random day")
    w(f"  {'horizon':>8} {'signal':>9} {'base':>9} {'edge':>9} {'sig>0':>8} {'base>0':>8} {'eff n':>7}")
    for sc in a["scores"]:
        if sc["signal_median"] is None:
            continue
        w(f"  {sc['horizon']:>7}d {_pct(sc['signal_median'], 8)} {_pct(sc['base_median'], 8)} "
          f"{sc['edge_pp']:>8.0f}pp {_pct(sc['signal_hit_rate'], 7)} "
          f"{_pct(sc['base_hit_rate'], 7)} {sc['effective_n']:>7.0f}")

    w("\nTIMING AND MINER STRESS AT EACH TROUGH (hashrate era only)")
    w(f"  {'trough':>12} {'depth':>8} {'hr drawdown':>13} {'pctile':>8} {'signal lag':>12}")
    for t in a["troughs"]:
        lag = (f"{t['signal_lag_days']:+d}d" if t["signal_lag_days"] is not None
               else f"none <{MAX_ASSOCIATION_DAYS}d")
        w(f"  {str(t['trough_date']):>12} {t['depth_pct']:>7.1f}% "
          f"{t['hashrate_drawdown']:>12.1f}% {t['percentile']:>7.0f}th {lag:>12}")

    w(f"\nFORWARD RETURNS BY HASHRATE-DRAWDOWN DECILE ({DRAWDOWN_WINDOW}d high)")
    hdr = " ".join(f"{str(h) + 'd':>9}" for h in horizons)
    w(f"  {'decile':>7} {'range':>18} {hdr} {'n':>6} {'eff n':>7}")
    for r in a["deciles"]:
        cells = " ".join(_pct(r["returns"].get(h), 8) for h in horizons)
        rng = f"{r['low']:.0f}% to {r['high']:.0f}%"
        w(f"  {r['decile']:>7} {rng:>18} {cells} {r['n']:>6} {r['effective_n']:>7.1f}")

    c = a["current"]
    w(f"\nCURRENT STATE ({c['date']})")
    w(f"  hashrate {c['hashrate_drawdown']:.1f}% off its {DRAWDOWN_WINDOW}d high "
      f"({c['percentile']:.0f}th percentile of all days)")
    if c["fast_ma"] and c["slow_ma"]:
        w(f"  ribbon {FAST_MA}d MA {c['fast_ma']:.1f} vs {SLOW_MA}d MA {c['slow_ma']:.1f} -> "
          f"{'IN CAPITULATION' if c['in_capitulation'] else 'recovered'}")
    w(f"  last signal: {c['last_signal']}")

    w("\nREADING THIS")
    w("  - The base column is not decoration. BTC's unconditional forward return")
    w("    over this sample is strongly positive, so any signal judged on its own")
    w("    absolute return looks good. Only the edge over base means anything.")
    w("  - 'eff n' is the number of non-overlapping forward windows. Daily rows")
    w("    are autocorrelated and their windows overlap, so a decile of ~380 days")
    w("    holds barely one independent 365d observation. Treat the medians as")
    w("    directional, never as estimates with the precision shown.")
    w(f"  - 'signal lag' is blank where no ribbon signal fell within")
    w(f"    {MAX_ASSOCIATION_DAYS} days of the trough. An unbounded 'nearest signal' always")
    w("    finds one, which manufactures a relationship out of a long gap.")
    w("  - Hashrate does not participate in every bottom. A credit or exchange")
    w("    failure can bottom price while hashrate barely moves, so a quiet")
    w("    reading here is not evidence against a bottom.")
    return "\n".join(out) + "\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="hashrate_study.py",
        description="Test whether hashrate-derived indicators mark cycle price bottoms.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--db", default=None, help="warehouse path (default: the tool's normal resolution)")
    p.add_argument("--json", action="store_true", help="emit the analysis as JSON")
    p.add_argument("--min-depth", type=float, default=DEFAULT_MIN_DEPTH,
                   help=f"drawdown %% that starts an episode (default {DEFAULT_MIN_DEPTH:.0f})")
    p.add_argument("--horizons", default=",".join(str(h) for h in DEFAULT_HORIZONS),
                   help="comma-separated forward horizons in days")
    args = p.parse_args(argv)

    try:
        horizons = tuple(int(h) for h in args.horizons.split(",") if h.strip())
    except ValueError:
        print(f"bad --horizons: {args.horizons!r}", file=sys.stderr)
        return 2
    if not horizons:
        print("--horizons needs at least one value", file=sys.stderr)
        return 2

    db = args.db
    if db is None:
        from btc_dashboard.config import Config
        db = Config.from_env().db_path

    try:
        series = load(db)
    except ImportError:
        print("duckdb is not installed", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"could not read the warehouse at {db}: {e}", file=sys.stderr)
        return 1

    analysis = analyse(series, horizons, args.min_depth)
    if args.json:
        print(json.dumps(analysis, indent=2, default=str))
    else:
        print(report(analysis, horizons), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
