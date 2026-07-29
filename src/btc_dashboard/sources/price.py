"""Live BTC spot price and its 200-day simple moving average.

Portable — needs only network access, so this is the one source that works
identically on a laptop and on the node host.

Two providers, tried in order. Each returns a list of daily closes oldest-first;
the SMA is computed here rather than taken from any provider, so the two are
directly comparable and a provider switch can't silently change the definition.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Any

from . import SourceResult, fmt, unavailable

NAME = "price"

SMA_WINDOW = 200
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


def _get(url: str, timeout: int) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _coingecko(timeout: int) -> list[float]:
    data = _get(COINGECKO, timeout)
    return [p[1] for p in data.get("prices", []) if isinstance(p[1], (int, float))]


def _binance(timeout: int) -> list[float]:
    return [float(k[4]) for k in _get(BINANCE, timeout)]


def classify(pct: float) -> str:
    if pct > NEAR_BAND_PCT:
        return "above"
    if pct >= -NEAR_BAND_PCT:
        return "near"
    return "below"


def collect(cfg) -> SourceResult:
    errors = []
    closes = source = None
    for label, fn in (("coingecko", _coingecko), ("binance", _binance)):
        try:
            closes = fn(cfg.timeout)
            source = label
            break
        except Exception as e:
            errors.append(f"{label}: {e}")
    if not closes:
        return unavailable(NAME, "; ".join(errors) or "no price source returned data")

    spot = closes[-1]
    # The last entry is today's in-progress candle on both providers, so the SMA
    # window excludes it. Averaging a partial day into a 200-day mean would let
    # an intraday move leak into the level that move is being measured against.
    window = closes[-(SMA_WINDOW + 1):-1] if len(closes) > SMA_WINDOW else []
    if len(window) < SMA_WINDOW:
        return SourceResult(
            name=NAME,
            available=True,
            data={
                "spot": round(spot, 2),
                "source": source,
                "sma200": None,
                "sma200_pct": None,
                "sma200_position": None,
                "days_available": len(closes),
            },
        )

    sma = sum(window) / len(window)
    pct = (spot - sma) / sma * 100
    return SourceResult(
        name=NAME,
        available=True,
        data={
            "spot": round(spot, 2),
            "source": source,
            "sma200": round(sma, 2),
            "sma200_pct": round(pct, 2),
            "sma200_position": classify(pct),
            "days_available": len(closes),
        },
    )


def render_lines(d: dict) -> list[str]:
    out = [f"spot {fmt(d.get('spot'), ',.0f', prefix='$')} ({d.get('source') or 'unknown'})"]
    if d.get("sma200") is not None:
        out.append(
            f"200d SMA {fmt(d.get('sma200'), ',.0f', prefix='$')} | "
            f"{fmt(d.get('sma200_pct'), '+.1f', suffix='%')} "
            f"({d.get('sma200_position') or 'n/a'})"
        )
    else:
        out.append(
            f"200d SMA n/a ({fmt(d.get('days_available'), missing='?')}d available)"
        )
    return out


def context_lines(d: dict) -> list[str]:
    out = []
    if d.get("spot") is not None:
        out.append(f"BTC spot: {fmt(d.get('spot'), ',.0f', prefix='$')}")
    if d.get("sma200") is not None:
        out.append(
            f"BTC 200d SMA: {fmt(d.get('sma200'), ',.0f', prefix='$')} | price is "
            f"{fmt(d.get('sma200_pct'), '+.1f', suffix='%')} vs SMA "
            f"({d.get('sma200_position') or 'position unknown'} the 200d)"
        )
    return out
