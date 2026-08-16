"""Live BTC spot price and its 200-day simple moving average.

Portable — needs only network access, so this is the one source that works
identically on a laptop and on the node host.

Two providers, tried in order. Each returns a list of daily closes oldest-first;
the SMA is computed here rather than taken from any provider, so the two are
directly comparable and a provider switch can't silently change the definition.
"""
from __future__ import annotations

import datetime
import json
import urllib.request
from typing import Any

from . import Metric, Panel, SourceResult, fmt, unavailable

NAME = "price"

# Short, intermediate, and long trend. The last is the primary: it drives the
# above/near/below classifier and the legacy flat `sma200*` fields.
SMA_WINDOWS = (20, 50, 200)
PRIMARY_SMA = SMA_WINDOWS[-1]
UA = "btc_dashboard/0.1 (+https://github.com/)"

COINGECKO = (
    "https://api.coingecko.com/api/v3/coins/bitcoin/market_chart"
    "?vs_currency=usd&days=201&interval=daily"
)
BINANCE = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1d&limit=202"

# Percent bands around the SMA. Inside +/-2% the level is close enough that
# calling it "support" or "resistance" overstates the signal, so it gets its own
# label rather than being forced into one side.
NEAR_BAND_PCT = 2.0

# Moves smaller than this against the previous close are left uncoloured.
# Current daily volatility puts one standard deviation near 1%, so a tenth of
# that is indistinguishable from intraday noise, and painting it green or red
# asserts a direction the number does not carry.
NEUTRAL_BAND_PCT = 0.1


def _get(url: str, timeout: int) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _coingecko(timeout: int) -> list[tuple[datetime.date, float]]:
    """`(closing day, price)` pairs, oldest first.

    CoinGecko's daily points are instants stamped at 00:00 UTC, so the point
    labelled 16 Aug is the price at that boundary — which is the *close of
    15 Aug*. Attributing it to the 16th would date every close a day late.
    Taking the day that just ended handles the final point too, which is a live
    quote at an arbitrary time and belongs to today.
    """
    data = _get(COINGECKO, timeout)
    out = []
    for ts, px in data.get("prices", []):
        if not isinstance(px, (int, float)):
            continue
        moment = datetime.datetime.fromtimestamp(ts / 1000, datetime.timezone.utc)
        out.append(((moment - datetime.timedelta(seconds=1)).date(), float(px)))
    return out


def _binance(timeout: int) -> list[tuple[datetime.date, float]]:
    """`(closing day, price)` pairs, oldest first.

    The opposite convention to CoinGecko: a kline stamped with the *open* time
    of a day carries that day's close in field 4, so the date is used as-is.
    """
    out = []
    for k in _get(BINANCE, timeout):
        day = datetime.datetime.fromtimestamp(
            int(k[0]) / 1000, datetime.timezone.utc).date()
        out.append((day, float(k[4])))
    return out


def classify(pct: float) -> str:
    if pct > NEAR_BAND_PCT:
        return "above"
    if pct >= -NEAR_BAND_PCT:
        return "near"
    return "below"


def collect(cfg) -> SourceResult:
    errors = []
    series = source = None
    for label, fn in (("coingecko", _coingecko), ("binance", _binance)):
        try:
            series = fn(cfg.timeout)
            source = label
            break
        except Exception as e:
            errors.append(f"{label}: {e}")
    if not series:
        return unavailable(NAME, "; ".join(errors) or "no price source returned data")

    closes = [px for _, px in series]
    spot = closes[-1]
    # The last entry is today's in-progress candle on both providers, so every
    # SMA window excludes it. Averaging a partial day into a mean would let an
    # intraday move leak into the level that move is being measured against.
    completed = closes[:-1]

    smas = [_sma(completed, spot, days) for days in SMA_WINDOWS]
    primary = next(s for s in smas if s["days"] == PRIMARY_SMA)

    # The previous completed daily close, from the same provider as spot.
    # Deliberately not the warehouse's close: that is a different venue, and
    # comparing a CoinGecko spot against a Kraken close would put a venue
    # spread into a figure meant to show the day's move.
    prev_close = completed[-1] if completed else None
    # Dated, because the warehouse's "daily close" is a different day whenever
    # the ingester has not yet run — an undated reference makes that ordinary
    # one-day lag look like the two sources disagreeing about the price.
    prev_close_date = series[-2][0] if len(series) > 1 else None
    change_pct = (
        round((spot - prev_close) / prev_close * 100, 2)
        if prev_close else None
    )

    return SourceResult(
        name=NAME,
        available=True,
        data={
            "spot": round(spot, 2),
            "source": source,
            # Named for what it is. NOT a 24-hour change: it is measured from
            # the last completed daily close, which may be one hour ago or
            # twenty-three depending on when the tool runs.
            "prev_close": round(prev_close, 2) if prev_close else None,
            "prev_close_date": prev_close_date.isoformat() if prev_close_date else None,
            "change_pct": change_pct,
            "smas": smas,
            # Flat aliases for the primary window, kept so an existing consumer
            # of the schema keeps working while `smas` becomes the general form.
            "sma200": primary["value"],
            "sma200_pct": primary["pct"],
            "sma200_position": primary["position"],
            "days_available": len(completed),
        },
    )


def _sma(completed: list[float], spot: float, days: int) -> dict:
    """One simple moving average and the spot price's distance from it.

    An unfillable window reports `covered: False` with null figures rather than
    averaging whatever is available — a 60-day mean labelled "200d" is worse
    than no answer, because it looks like one.
    """
    window = completed[-days:] if len(completed) >= days else []
    if not window:
        return {
            "days": days,
            "days_available": len(completed),
            "covered": False,
            "value": None,
            "pct": None,
            "position": None,
        }
    value = sum(window) / days
    pct = (spot - value) / value * 100
    return {
        "days": days,
        "days_available": len(completed),
        "covered": True,
        "value": round(value, 2),
        "pct": round(pct, 2),
        "position": classify(pct),
    }


def _sma_entries(d: dict) -> list[dict]:
    """The `smas` list, reconstructed from the flat aliases if it is absent.

    A snapshot produced before `smas` existed still renders, rather than losing
    the SMA line entirely.
    """
    entries = d.get("smas")
    if isinstance(entries, list) and entries:
        return [e for e in entries if isinstance(e, dict)]
    if d.get("sma200") is None:
        return []
    return [{
        "days": PRIMARY_SMA, "covered": True, "value": d.get("sma200"),
        "pct": d.get("sma200_pct"), "position": d.get("sma200_position"),
        "days_available": d.get("days_available"),
    }]


def _close_label(d: dict) -> str:
    """`14 Aug close` where the date is known, else a bare `prev close`."""
    raw = d.get("prev_close_date")
    try:
        return f"{datetime.date.fromisoformat(raw):%-d %b} close"
    except (TypeError, ValueError):
        return "prev close"


def change_tone(pct) -> str | None:
    """up / down / None, with a dead band around zero."""
    if not isinstance(pct, (int, float)) or abs(pct) < NEUTRAL_BAND_PCT:
        return None
    return "up" if pct > 0 else "down"


def render_lines(d: dict) -> list[str]:
    chg = (f" {fmt(d.get('change_pct'), '+.2f', suffix='%')} vs "
           f"{_close_label(d)}" if d.get("change_pct") is not None else "")
    out = [f"spot {fmt(d.get('spot'), ',.0f', prefix='$')}{chg} "
           f"({d.get('source') or 'unknown'})"]

    parts = []
    for s in _sma_entries(d):
        if s.get("covered"):
            parts.append(
                f"{fmt(s.get('days'))}d {fmt(s.get('value'), ',.0f', prefix='$')} "
                f"{fmt(s.get('pct'), '+.1f', suffix='%')}"
            )
        else:
            parts.append(
                f"{fmt(s.get('days'))}d n/a "
                f"({fmt(s.get('days_available'), missing='?')}d)"
            )
    if parts:
        # The classifier rides the primary window only — it is the regime
        # marker, and repeating above/near/below three times reads as noise.
        pos = d.get("sma200_position")
        out.append("SMA " + " | ".join(parts) + (f" ({pos} 200d)" if pos else ""))
    return out


def context_lines(d: dict) -> list[str]:
    out = []
    if d.get("spot") is not None:
        line = f"BTC spot: {fmt(d.get('spot'), ',.0f', prefix='$')}"
        if d.get("change_pct") is not None:
            line += (
                f", {fmt(d.get('change_pct'), '+.2f', suffix='%')} against the "
                f"{_close_label(d)} of "
                f"{fmt(d.get('prev_close'), ',.0f', prefix='$')}. That is not a "
                f"24-hour change — the reference is the last finished day, which "
                f"may be an hour or a day old depending on when this ran. It is "
                f"also a different day from the warehouse's daily close whenever "
                f"the ingester has not yet run, so do not read a gap between the "
                f"two as the sources disagreeing."
            )
        out.append(line)
    for s in _sma_entries(d):
        days = fmt(s.get("days"))
        if s.get("covered"):
            out.append(
                f"BTC {days}d SMA: {fmt(s.get('value'), ',.0f', prefix='$')}, spot is "
                f"{fmt(s.get('pct'), '+.1f', suffix='%')} vs it "
                f"({s.get('position') or 'position unknown'})"
            )
        else:
            out.append(
                f"BTC {days}d SMA: not available — only "
                f"{fmt(s.get('days_available'), missing='an unknown number of')} "
                f"completed days were returned. Do not treat this as zero or "
                f"substitute a shorter average."
            )
    return out


def html_panels(d: dict) -> list[Panel]:
    chg, prev = d.get("change_pct"), d.get("prev_close")
    note = d.get("source") or "unknown"
    if chg is not None:
        note = (f"{fmt(chg, '+.2f', suffix='%')} vs {_close_label(d)} "
                f"{fmt(prev, ',.0f', prefix='$')} · {note}")
    rows = [Metric("Spot", fmt(d.get("spot"), ",.0f", prefix="$"),
                   note=note, tone=change_tone(chg))]
    for s in _sma_entries(d):
        days = fmt(s.get("days"))
        if s.get("covered"):
            pct = s.get("pct")
            # The tone rides the note: what is signed is spot's distance from
            # the average, not the average itself.
            rows.append(Metric(
                f"{days}D SMA", fmt(s.get("value"), ",.0f", prefix="$"),
                note=f"spot {fmt(pct, '+.1f', suffix='%')} vs it",
                note_tone=change_tone(pct) or (
                    "up" if isinstance(pct, (int, float)) and pct >= 0 else "down"),
            ))
        else:
            rows.append(Metric(
                f"{days}D SMA", "n/a",
                note=f"only {fmt(s.get('days_available'), missing='?')} days available",
            ))
    return [Panel("PRICE", rows, priority=10)]


# A day's move worth calling out. Roughly three standard deviations at the
# volatility of a quiet market and about one at a turbulent one, so it fires
# rarely now and rarely then — which is what a "notable" list needs.
NOTABLE_MOVE_PCT = 3.0


def notable(d: dict) -> list[str]:
    chg = d.get("change_pct")
    if isinstance(chg, (int, float)) and abs(chg) >= NOTABLE_MOVE_PCT:
        return [f"spot {fmt(chg, '+.1f', suffix='%')} vs the {_close_label(d)}"]
    return []
