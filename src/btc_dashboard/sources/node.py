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

from . import SourceResult, unavailable

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


def render_lines(d: dict) -> list[str]:
    hr = f"hashrate {d['hash_rate_ehs']:,.2f} EH/s"
    if d.get("hash_rate_7d_pct") is not None:
        hr += f" ({d['hash_rate_7d_pct']:+.2f}% 7d)"
    rt = d["retarget"]
    proj = (
        f"proj {rt['projection_pct']:+.2f}%"
        if rt["projection_pct"] is not None
        else f"proj n/a ({rt['blocks_elapsed']} blks into period)"
    )
    eta = f", ~{rt['eta_days']}d" if rt["eta_days"] is not None else ""
    f = d["fees_sat_vb"]
    fee_txt = "/".join("n/a" if f[k] is None else f"{f[k]:g}" for k in ("fast", "hour", "day"))
    return [
        f"height {d['height']:,} | {hr} | difficulty {d['difficulty_t']:,.2f}T",
        f"retarget {rt['blocks_left']} blks{eta} | {proj}",
        f"mempool {d['mempool']['tx']:,} tx / {d['mempool']['vmb']:.1f} vMB",
        f"fees {fee_txt} sat/vB (fast/1hr/1d)",
    ]


def context_lines(d: dict) -> list[str]:
    out = [f"BTC live hash rate: {d['hash_rate_ehs']:,.2f} EH/s"]
    if d.get("hash_rate_7d_pct") is not None:
        out.append(f"BTC hash rate 7d change: {d['hash_rate_7d_pct']:+.2f}%")
    rt = d["retarget"]
    if rt["projection_pct"] is not None:
        out.append(
            f"BTC difficulty retarget projection: {rt['projection_pct']:+.2f}% in "
            f"{rt['blocks_left']} blocks — a miner-pressure signal"
        )
    out.append(
        f"BTC mempool: {d['mempool']['tx']:,} tx / {d['mempool']['vmb']:.1f} vMB "
        f"(live, not a daily average)"
    )
    return out
