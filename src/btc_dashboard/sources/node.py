"""Live network state, straight from a Bitcoin Core node via `bitcoin-cli`.

Scope is deliberately the *tip*: hashrate, difficulty, the retarget projection,
mempool depth, and fee estimates. Daily aggregates (block fullness, fee/subsidy,
miner revenue) come from the warehouse instead — they need a full UTC day to
mean anything, and walking a day of blocks per invocation would make the CLI
slow for a number that is already stored.

Requires a synced node, so this is the source that goes unavailable on a laptop.
That is reported, not hidden: a missing node means the live block of the
snapshot is absent, and both the reader and the analyst are told so.
"""
from __future__ import annotations

import json
import subprocess

from . import Metric, Panel, SourceResult, fmt, unavailable

NAME = "node"

# Window for the hashrate estimate, in blocks. 1008 is one week at target pace —
# long enough that variance in block discovery doesn't dominate the number.
HASHRATE_WINDOW = 1008
RETARGET_INTERVAL = 2016
# Below this many blocks into a difficulty period the cumulative projection is
# single-block noise: early on, one fast block can imply an absurd adjustment.
MIN_BLOCKS_FOR_PROJ = 144


class NodeError(RuntimeError):
    pass


def _cli(cfg, *args: str):
    """Run one bitcoin-cli call and parse its output.

    Core emits JSON for structured results and bare scalars for others, so a
    failed JSON parse is a value, not an error.
    """
    try:
        proc = subprocess.run(
            [cfg.bitcoin_cli, *args],
            capture_output=True, text=True, timeout=cfg.timeout,
        )
    except FileNotFoundError:
        raise NodeError(f"{cfg.bitcoin_cli} not found on PATH")
    except subprocess.TimeoutExpired:
        raise NodeError(f"{cfg.bitcoin_cli} {' '.join(args)} timed out")
    if proc.returncode != 0:
        raise NodeError((proc.stderr or proc.stdout).strip() or "bitcoin-cli failed")
    out = proc.stdout.strip()
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return out


def _satvb(est: dict) -> float | None:
    """estimatesmartfee returns BTC/kvB; convert to sat/vB."""
    fr = est.get("feerate") if isinstance(est, dict) else None
    return round(fr * 1e5, 1) if fr is not None else None


def collect(cfg) -> SourceResult:
    try:
        tip = int(_cli(cfg, "getblockcount"))
        hr_now = float(_cli(cfg, "getnetworkhashps", str(HASHRATE_WINDOW)))
        hr_old = float(
            _cli(cfg, "getnetworkhashps", str(HASHRATE_WINDOW), str(tip - HASHRATE_WINDOW))
        )
        difficulty = float(_cli(cfg, "getdifficulty"))
        mempool = _cli(cfg, "getmempoolinfo")

        period_start = tip - tip % RETARGET_INTERVAL
        start_hdr = _cli(cfg, "getblockheader", _cli(cfg, "getblockhash", str(period_start)))
        tip_hdr = _cli(cfg, "getblockheader", _cli(cfg, "getblockhash", str(tip)))

        elapsed = tip - period_start
        blocks_left = RETARGET_INTERVAL - tip % RETARGET_INTERVAL
        proj = eta_days = None
        if elapsed > 0 and tip_hdr["time"] > start_hdr["time"]:
            pace = (tip_hdr["time"] - start_hdr["time"]) / elapsed
            eta_days = round(blocks_left * pace / 86400, 1)
            if elapsed >= MIN_BLOCKS_FOR_PROJ:
                proj = round((600 / pace - 1) * 100, 2)

        fees = {
            "fast": _satvb(_cli(cfg, "estimatesmartfee", "2")),
            "hour": _satvb(_cli(cfg, "estimatesmartfee", "6")),
            "day": _satvb(_cli(cfg, "estimatesmartfee", "144")),
        }

        return SourceResult(
            name=NAME,
            available=True,
            data={
                "height": tip,
                "hash_rate_ehs": round(hr_now / 1e18, 2),
                "hash_rate_7d_pct": round((hr_now - hr_old) / hr_old * 100, 2)
                if hr_old
                else None,
                "difficulty_t": round(difficulty / 1e12, 2),
                "retarget": {
                    "blocks_left": blocks_left,
                    "blocks_elapsed": elapsed,
                    "eta_days": eta_days,
                    # None when too early in the period to be meaningful — the
                    # warehouse's day-pace figure is the fallback in that case.
                    "projection_pct": proj,
                },
                "mempool": {
                    "tx": mempool.get("size"),
                    "vmb": round(mempool.get("bytes", 0) / 1e6, 1),
                },
                "fees_sat_vb": fees,
            },
        )
    except NodeError as e:
        return unavailable(NAME, str(e))
    except Exception as e:
        return unavailable(NAME, f"{type(e).__name__}: {e}")


def _fee(v) -> str:
    """Fee rate for display, at a consistent width.

    A `g` format drops the trailing zero, so 4.0 rendered as `4` while its
    neighbours kept a decimal and the three didn't line up. Below 10 sat/vB one
    decimal is meaningful; above it, the tenths are noise on an estimate.
    """
    if not isinstance(v, (int, float)):
        return "n/a"
    return fmt(v, ".1f") if v < 10 else fmt(v, ".0f")


def render_lines(d: dict) -> list[str]:
    hr = f"hashrate {fmt(d.get('hash_rate_ehs'), ',.2f')} EH/s"
    if d.get("hash_rate_7d_pct") is not None:
        hr += f" ({fmt(d.get('hash_rate_7d_pct'), '+.2f', suffix='%')} 7d)"

    rt = d.get("retarget") or {}
    if rt.get("projection_pct") is not None:
        proj = f"proj {fmt(rt.get('projection_pct'), '+.2f', suffix='%')}"
    else:
        proj = f"proj n/a ({fmt(rt.get('blocks_elapsed'), missing='?')} blks into period)"
    eta = f", ~{fmt(rt.get('eta_days'))}d" if rt.get("eta_days") is not None else ""

    f = d.get("fees_sat_vb") or {}
    fee_txt = "/".join(_fee(f.get(k)) for k in ("fast", "hour", "day"))

    mp = d.get("mempool") or {}
    return [
        f"height {fmt(d.get('height'), ',')} | {hr} | "
        f"difficulty {fmt(d.get('difficulty_t'), ',.2f')}T",
        f"retarget {fmt(rt.get('blocks_left'), ',')} blks{eta} | {proj}",
        f"mempool {fmt(mp.get('tx'), ',')} tx / {fmt(mp.get('vmb'), '.1f')} vMB",
        f"fees {fee_txt} sat/vB (fast/1hr/1d)",
    ]


def context_lines(d: dict) -> list[str]:
    out = []
    if d.get("hash_rate_ehs") is not None:
        out.append(f"BTC live hash rate: {fmt(d.get('hash_rate_ehs'), ',.2f')} EH/s")
    if d.get("hash_rate_7d_pct") is not None:
        out.append(
            f"BTC hash rate 7d change: {fmt(d.get('hash_rate_7d_pct'), '+.2f', suffix='%')}"
        )
    rt = d.get("retarget") or {}
    if rt.get("projection_pct") is not None:
        out.append(
            f"BTC difficulty retarget projection: "
            f"{fmt(rt.get('projection_pct'), '+.2f', suffix='%')} in "
            f"{fmt(rt.get('blocks_left'), ',')} blocks — a miner-pressure signal"
        )
    mp = d.get("mempool") or {}
    if mp.get("tx") is not None or mp.get("vmb") is not None:
        out.append(
            f"BTC mempool: {fmt(mp.get('tx'), ',')} tx / {fmt(mp.get('vmb'), '.1f')} vMB "
            f"(live, not a daily average)"
        )
    return out


def html_panels(d: dict) -> list[Panel]:
    rt = d.get("retarget") or {}
    mp = d.get("mempool") or {}
    f = d.get("fees_sat_vb") or {}
    hr7 = d.get("hash_rate_7d_pct")

    proj = rt.get("projection_pct")
    return [Panel("NETWORK (LIVE)", [
        Metric("Block Height", fmt(d.get("height"), ",")),
        Metric("Hashrate", f"{fmt(d.get('hash_rate_ehs'), ',.0f')} EH/s",
               note=f"{fmt(hr7, '+.2f', suffix='%')} over 7d" if hr7 is not None else None,
               tone="up" if isinstance(hr7, (int, float)) and hr7 >= 0 else "down"),
        Metric("Difficulty", f"{fmt(d.get('difficulty_t'), ',.2f')} T"),
        Metric("Next Retarget",
               fmt(proj, "+.2f", suffix="%") if proj is not None else "n/a",
               note=(f"{fmt(rt.get('blocks_left'), ',')} blks"
                     + (f", ~{fmt(rt.get('eta_days'))}d" if rt.get("eta_days") is not None else "")
                     + ("" if proj is not None else " — too early to project")),
               tone="up" if isinstance(proj, (int, float)) and proj >= 0 else "down"),
        Metric("Mempool", f"{fmt(mp.get('vmb'), '.1f')} vMB",
               note=f"{fmt(mp.get('tx'), ',')} tx"),
        Metric("Fee Estimates",
               "/".join(_fee(f.get(k)) for k in ("fast", "hour", "day")) + " sat/vB",
               note="fast / 1hr / 1day"),
    ])]
