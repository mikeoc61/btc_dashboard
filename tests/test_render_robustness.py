"""Renderers must never raise, whatever is missing from a source's data.

A renderer that raises costs the whole block — that is how a `close` field
that was always None (read from the wrong table) turned into
"render failed: TypeError" for the entire ON-CHAIN section.

These tests take a fully-populated payload per source and knock out each field
in turn, plus every field at once, asserting the renderer still returns lines.
"""
from __future__ import annotations

import pytest

from btc_dashboard.sources import flows, node, price, warehouse

FULL = {
    "price": {
        "spot": 63939.0, "source": "coingecko", "sma200": 71840.0,
        "sma200_pct": -11.0, "sma200_position": "below", "days_available": 201,
    },
    "node": {
        "height": 960168, "hash_rate_ehs": 876.61, "hash_rate_7d_pct": -0.22,
        "difficulty_t": 126.23,
        "retarget": {"blocks_left": 1464, "blocks_elapsed": 552,
                     "eta_days": 10.7, "projection_pct": -5.14},
        "mempool": {"tx": 29001, "vmb": 31.9},
        "fees_sat_vb": {"fast": 4.2, "hour": 2.3, "day": 0.7},
    },
    "warehouse": {
        "date": "2026-07-28",
        "onchain": {"blocks_day": 147, "block_fullness": 96.0, "p50_fee": 2.0,
                    "fee_subsidy": 0.84, "miner_rev": 452.1,
                    "hash_rate_ehs": 870.0, "difficulty_t": 126.0, "tx_rate": 6.5},
        "signals": {"fee_pctile": 3.0, "apathy_days": 41,
                    "hashrate_drawdown": -4.2, "vol_pctile": 97.5},
        "close": 63900.0, "sma200": 71840.0, "sma200_pct": -11.0,
        "day_pace_retarget": -1.2, "days_behind": 1, "warehouse_stale": False,
    },
    "flows": {
        "lead": "IBIT", "as_of": "28 Jul 2026", "age_days": 1,
        "latest_total": -49.7, "latest_lead": -54.8, "days_complete": 638,
        "windows": [
            {"days": 5, "days_available": 5, "covered": True,
             "total": -457.4, "lead": -439.5},
            {"days": 20, "days_available": 12, "covered": False,
             "total": None, "lead": None},
        ],
        "streak_days": 4, "streak_sign": "outflow", "regime": "conviction",
        "partial": {"date": "29 Jul 2026", "reported_total": -10.0, "other": 1.0,
                    "reported": ["IBIT"], "pending": ["FBTC", "ARKB", "GBTC"]},
    },
}

MODULES = {"price": price, "node": node, "warehouse": warehouse, "flows": flows}


def _blank(value):
    """Same shape, every leaf None — simulates a source that returned nothing."""
    if isinstance(value, dict):
        return {k: _blank(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_blank(v) for v in value]
    return None


@pytest.mark.parametrize("name", sorted(MODULES))
def test_renders_fully_populated_data(name):
    lines = MODULES[name].render_lines(FULL[name])
    assert lines and all(isinstance(x, str) for x in lines)


@pytest.mark.parametrize("name", sorted(MODULES))
def test_survives_each_top_level_field_being_none(name):
    for field in FULL[name]:
        data = dict(FULL[name])
        data[field] = None
        try:
            MODULES[name].render_lines(data)
        except Exception as e:
            pytest.fail(f"{name}.render_lines raised with {field}=None: {e!r}")


@pytest.mark.parametrize("name", sorted(MODULES))
def test_survives_each_nested_field_being_none(name):
    for field, value in FULL[name].items():
        if not isinstance(value, dict):
            continue
        for inner in value:
            data = dict(FULL[name])
            data[field] = dict(value, **{inner: None})
            try:
                MODULES[name].render_lines(data)
            except Exception as e:
                pytest.fail(
                    f"{name}.render_lines raised with {field}.{inner}=None: {e!r}"
                )


@pytest.mark.parametrize("name", sorted(MODULES))
def test_survives_everything_being_none(name):
    try:
        MODULES[name].render_lines(_blank(FULL[name]))
    except Exception as e:
        pytest.fail(f"{name}.render_lines raised on an all-None payload: {e!r}")


@pytest.mark.parametrize("name", sorted(MODULES))
def test_context_lines_survive_everything_being_none(name):
    """The analyst path has the same exposure — a raise there loses the facts."""
    try:
        MODULES[name].context_lines(_blank(FULL[name]))
    except Exception as e:
        pytest.fail(f"{name}.context_lines raised on an all-None payload: {e!r}")
