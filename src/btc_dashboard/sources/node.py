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
import math
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


def projection_sigma(blocks_elapsed) -> float | None:
    """One standard error on the retarget projection, as a percentage.

    Block discovery is Poisson, so the time to find n blocks has relative
    standard deviation 1/sqrt(n); the projection is a function of that elapsed
    time, so it inherits the same relative error. Nothing about the difficulty
    algorithm enters — this is the precision of the *estimate*, not a claim
    about where the adjustment will land.

    That error shrinks from ±8.3% at the `MIN_BLOCKS_FOR_PROJ` floor to
    ±2.2% at a full period, which is the whole reason it has to be carried
    beside the level. +5.7% at 323 blocks in is one standard error and means
    nothing; the same +5.7% at 1,700 is four and means hashrate has genuinely
    moved. A reader shown only the level cannot tell those apart, and neither
    can a model.
    """
    if not isinstance(blocks_elapsed, int) or blocks_elapsed <= 0:
        return None
    return round(100 / math.sqrt(blocks_elapsed), 2)


def _sigma(rt: dict) -> float | None:
    """The carried band, or one recovered from the block count behind it.

    Recovered rather than dropped because `blocks_elapsed` predates the band
    in the schema: an ingested snapshot written before this existed still holds
    everything the qualifier is computed from, and a qualifier that can be
    reconstructed should never be silently absent.
    """
    s = rt.get("projection_sigma_pct")
    if isinstance(s, (int, float)) and s > 0:
        return s
    return projection_sigma(rt.get("blocks_elapsed"))


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
        proj = eta_days = sigma = None
        if elapsed > 0 and tip_hdr["time"] > start_hdr["time"]:
            pace = (tip_hdr["time"] - start_hdr["time"]) / elapsed
            eta_days = round(blocks_left * pace / 86400, 1)
            if elapsed >= MIN_BLOCKS_FOR_PROJ:
                proj = round((600 / pace - 1) * 100, 2)
                sigma = projection_sigma(elapsed)

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
                    # Collected, not derived per consumer. Every presentation
                    # needs it and the threshold that selects for the NOTABLE
                    # strip is stated in multiples of it, so a qualifier each
                    # renderer had to recompute is one a renderer can forget.
                    "projection_sigma_pct": sigma,
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
        sigma = _sigma(rt)
        if sigma is not None:
            # The block count rides with the band, because the band is only
            # interpretable against it — and because the n/a branch below has
            # always shown the count for exactly the same reason.
            proj += (f" ±{fmt(sigma, '.1f')}% "
                     f"({fmt(rt.get('blocks_elapsed'), missing='?')} blks in)")
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
    proj = rt.get("projection_pct")
    if proj is not None:
        line = (
            f"BTC difficulty retarget projection: "
            f"{fmt(proj, '+.2f', suffix='%')} in "
            f"{fmt(rt.get('blocks_left'), ',')} blocks — a miner-pressure signal"
        )
        sigma = _sigma(rt)
        if sigma is not None and isinstance(proj, (int, float)):
            # Spelled out, and the ratio computed here rather than left for the
            # model to work out. The projection is a pace estimate over the
            # blocks found so far, and a model handed the level alone reads an
            # early-period wobble as a hashrate story — it has no way to know
            # the reading is inside its own error unless it is told in the
            # units that answer the question.
            line += (
                f", estimated from {fmt(rt.get('blocks_elapsed'), ',')} blocks so "
                f"far and therefore ±{fmt(sigma, '.1f')}% at one standard "
                f"error; this reading is {abs(proj) / sigma:.1f} s.e. from flat"
            )
        out.append(line)
    mp = d.get("mempool") or {}
    if mp.get("tx") is not None or mp.get("vmb") is not None:
        out.append(
            f"BTC mempool: {fmt(mp.get('tx'), ',')} tx / {fmt(mp.get('vmb'), '.1f')} vMB "
            f"(live, not a daily average)"
        )
    return out


# Colour asserts a sign, so the sign has to be real. One standard error, not
# the strip's two: the bar for "this direction is probably not noise" is lower
# than for "lead the page with it", and at 2σ the colour would say nothing the
# NOTABLE strip had not already said.
NEUTRAL_BAND_SIGMA = 1.0


def retarget_tone(rt: dict) -> str | None:
    """up / down / None, with a dead band that widens early in a period.

    `price.change_tone` does this job against a fixed percentage, which works
    there because daily volatility is roughly stable. It cannot work here: the
    projection's own error runs from ±8.3% to ±2.2% across a period, so one
    percentage would be a different test at each end — strict enough to silence
    a real move late, loose enough to paint noise green early.

    The `None` also covers the value being absent, which is the other half of
    what this replaces. `n/a` fell to the `else` of a sign test and rendered
    red, reading as a projected *fall* rather than as no projection at all.
    """
    proj, sigma = rt.get("projection_pct"), _sigma(rt)
    if not isinstance(proj, (int, float)) or sigma is None:
        return None
    if abs(proj) < NEUTRAL_BAND_SIGMA * sigma:
        return None
    return "up" if proj > 0 else "down"


def html_panels(d: dict) -> list[Panel]:
    rt = d.get("retarget") or {}
    mp = d.get("mempool") or {}
    f = d.get("fees_sat_vb") or {}
    hr7 = d.get("hash_rate_7d_pct")

    proj = rt.get("projection_pct")
    sigma = _sigma(rt)
    # Blocks left and the ETA describe the *period*; the band describes the
    # *projection*. Both belong in the note, but only the band makes the value
    # above it comparable — and the block count has to travel with the band,
    # since the card otherwise reports blocks left and the reader would have to
    # subtract from 2016 to see how far in the estimate is.
    retarget_note = (
        f"{fmt(rt.get('blocks_left'), ',')} blks"
        + (f", ~{fmt(rt.get('eta_days'))}d" if rt.get("eta_days") is not None else "")
    )
    if proj is None:
        retarget_note += " — too early to project"
    elif sigma is not None:
        retarget_note += (f" · ±{fmt(sigma, '.1f')}% at "
                          f"{fmt(rt.get('blocks_elapsed'), ',')} blks in")
    return [Panel("NETWORK (LIVE)", priority=30, metrics=[
        Metric("Block Height", fmt(d.get("height"), ",")),
        # Tone on the note: the 7-day change is signed, the hashrate is not.
        Metric("Hashrate", f"{fmt(d.get('hash_rate_ehs'), ',.0f')} EH/s",
               note=f"{fmt(hr7, '+.2f', suffix='%')} over 7d" if hr7 is not None else None,
               note_tone=("up" if isinstance(hr7, (int, float)) and hr7 >= 0
                          else "down") if hr7 is not None else None),
        Metric("Difficulty", f"{fmt(d.get('difficulty_t'), ',.2f')} T"),
        Metric("Next Retarget",
               fmt(proj, "+.2f", suffix="%") if proj is not None else "n/a",
               note=retarget_note,
               tone=retarget_tone(rt)),
        Metric("Mempool", f"{fmt(mp.get('vmb'), '.1f')} vMB",
               note=f"{fmt(mp.get('tx'), ',')} tx"),
        Metric("Fee Estimates",
               "/".join(_fee(f.get(k)) for k in ("fast", "hour", "day")) + " sat/vB",
               note="fast / 1hr / 1day"),
    ])]


def balance_rows(d: dict) -> list[Metric]:
    """This source's one row on the balance card: the network's direction.

    The 7-day hashrate change rather than the retarget projection, though the
    projection is the livelier number. The projection's precision moves by a
    factor of four across a period — ±8.3% early, ±2.2% late — so a row showing
    it would have to carry its own error band to mean anything, and a band in a
    six-row digest is a footnote on a footnote. The 7-day change is estimated
    over a fixed `HASHRATE_WINDOW` and means the same thing every day, which is
    what a row on this card has to do. The projection keeps its place on the
    NETWORK card and on the strip above, where it is stated with its band.

    Survives an empty dict with its label intact, so a dead node still occupies
    its row.
    """
    hr7 = d.get("hash_rate_7d_pct")
    if not isinstance(hr7, (int, float)):
        return [Metric("Network", "n/a", note="no hashrate estimate")]
    return [Metric(
        "Network", fmt(hr7, "+.2f", suffix="%"),
        note=f"hashrate over {HASHRATE_WINDOW} blocks (~7d)",
        # Same rule as the NETWORK card: the level is not signed, its change
        # is, and here the change is the value.
        tone="up" if hr7 >= 0 else "down",
    )]


# How many standard errors a projection must clear to lead the page.
#
# Stated in multiples of its own error rather than as a fixed percentage,
# because that error moves by a factor of four across a period: ±8.3% at the
# `MIN_BLOCKS_FOR_PROJ` floor, ±2.2% at a full 2016 blocks. The 5% constant
# this replaces was inside the noise for the first third of every period — on
# 7 Sep 2026 it put "+5.7%" at the top of the page off 323 blocks, where one
# standard error is 5.6%, so the strip led with a reading indistinguishable
# from on-pace — and over-conservative for the last third, where a 5% move is
# better than two standard errors and unambiguously real.
#
# Two, not three: this selects a reading worth looking at, not one worth
# acting on, and at three the strip would have almost nothing to say until a
# period was nearly over.
NOTABLE_RETARGET_SIGMA = 2.0


def notable(d: dict) -> list[str]:
    rt = d.get("retarget") or {}
    proj, sigma = rt.get("projection_pct"), _sigma(rt)
    if not isinstance(proj, (int, float)) or sigma is None:
        return []
    if abs(proj) < NOTABLE_RETARGET_SIGMA * sigma:
        return []
    # The band travels onto the strip with the level. Every other entry there
    # carries the window it was ranked against; this one carries the precision
    # it was estimated at, which is the same job — it is what stops "+7.5%"
    # being read as a number someone measured rather than one they projected.
    return [f"difficulty retarget projected {fmt(proj, '+.1f', suffix='%')} "
            f"— ±{fmt(sigma, '.1f')}% at "
            f"{fmt(rt.get('blocks_elapsed'), ',')} blks in"]
