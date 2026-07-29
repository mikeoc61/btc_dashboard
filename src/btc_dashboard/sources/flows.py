"""U.S. spot Bitcoin ETF net flows, scraped from Farside Investors.

Values are US$ millions; negative is net outflow.

Three properties here are not obvious and are the difference between a number
that means something and one that doesn't:

1. **A reported zero is not a missing cell.** Farside renders an unpublished
   figure as `-` or blank and a genuine zero flow as `0.0`. Collapsing the two
   turns "hasn't reported" into "reported no flow", which silently drags every
   average toward zero. Blank parses to None; only an explicit 0 is 0.0.

2. **A day counts only once every tracked fund has reported.** Funds post
   progressively through the afternoon, so a day read mid-session has a real
   but incomplete Total whose *sign* can still flip. Partial days are excluded
   from every latest/streak/window figure and surfaced separately.

3. **An unfillable window reports n/a, never a shorter sum.** A 60-day net
   computed over 40 available days is a 40-day net wearing a 60-day label —
   worse than no answer, because it looks like one.

Fetching goes through Chrome TLS impersonation: the site fingerprints clients,
and a stock urllib/requests handshake gets a challenge page rather than the
table.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import SourceResult, fmt, unavailable

NAME = "flows"

NAV_URL = "https://farside.co.uk/btc/"
ALL_DATA_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
LEAD = "IBIT"
FUNDS = ("IBIT", "FBTC", "ARKB", "GBTC")
WINDOWS = (5, 20, 60)
CACHE_NAME = "flows_btc.json"

DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Lead-fund share of the window total, as a proportion. Above 120% the other
# funds are net-offsetting the lead and the visible total understates what the
# flagship actually did; below 60% the move is broad rather than lead-driven.
CONVICTION_MIN = 0.60
OFFSETTING_MIN = 1.20


def parse_flow(s: str) -> float | None:
    """Parse one cell. Blank/`-` -> None (not reported), never 0.0."""
    s = s.strip().replace(",", "").replace("–", "-")
    if s in ("", "-"):
        return None
    neg = s.startswith("(") and s.endswith(")")
    try:
        v = float(s.strip("()"))
    except ValueError:
        return None
    return -v if neg else v


def fetch_html(url: str, timeout: int) -> str:
    try:
        from curl_cffi import requests as creq

        r = creq.get(url, headers=HEADERS, timeout=timeout, impersonate="chrome")
        r.raise_for_status()
        return r.text
    except ImportError:
        pass
    import requests

    sess = requests.Session()
    sess.headers.update(HEADERS)
    # Warm the root first so the session picks up cookies; a cold request
    # straight to the table URL is more likely to draw a challenge.
    sess.get("https://farside.co.uk/", timeout=timeout)
    r = sess.get(url, timeout=timeout)
    r.raise_for_status()
    return r.text


def parse_table(html: str) -> list[dict]:
    """Locate and parse the flow table.

    Columns are resolved by header name rather than position, so an added or
    reordered column shifts nothing. The table is found by looking for rows
    whose first cell is a `D MMM YYYY` date, which survives the page's
    surrounding markup changing.
    """
    from bs4 import BeautifulSoup

    want = (*FUNDS, "Total")
    soup = BeautifulSoup(html, "html.parser")
    for table in soup.find_all("table"):
        rows = [
            [c.get_text(strip=True) for c in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        first = next((i for i, c in enumerate(rows) if c and DATE_RE.match(c[0])), None)
        if first is None:
            continue
        colmap: dict[str, int] = {}
        for cells in rows[:first]:
            for idx, text in enumerate(cells):
                if text in want and text not in colmap:
                    colmap[text] = idx
        if LEAD not in colmap or "Total" not in colmap:
            continue
        out = []
        for cells in rows[first:]:
            if not (cells and DATE_RE.match(cells[0])):
                continue
            rec: dict = {"date": cells[0]}
            for col in want:
                i = colmap.get(col)
                rec[col] = parse_flow(cells[i]) if i is not None and i < len(cells) else None
            out.append(rec)
        if out:
            return out
    raise ValueError("flow table not found — page layout may have changed")


def _other(row: dict) -> float | None:
    """Net of the funds inside Total that we don't itemize.

    Farside's Total sums every listed ETF; we track a curated subset. Without
    this residual the per-fund breakdown wouldn't reconcile to Total, and the
    gap invites being misread as a pending fund's value. It isn't — it's the
    untracked funds.
    """
    total = row.get("Total")
    if total is None:
        return None
    return round(total - sum(row[f] for f in FUNDS if row.get(f) is not None), 1)


def _window(complete: list[dict], days: int) -> dict:
    recent = complete[-days:] if days > 0 else []
    covered = len(recent) == days
    return {
        "days": days,
        "days_available": len(recent),
        "covered": covered,
        "total": round(sum(r["Total"] for r in recent if r["Total"] is not None), 1)
        if covered
        else None,
        "lead": round(sum(r[LEAD] for r in recent if r[LEAD] is not None), 1)
        if covered
        else None,
    }


def _streak(complete: list[dict]) -> tuple[int, str]:
    sign = None
    n = 0
    for r in reversed(complete):
        v = r["Total"]
        if v is None:
            break
        s = 1 if v > 0 else (-1 if v < 0 else 0)
        if sign is None:
            sign, n = s, 1
        elif s == sign and s != 0:
            n += 1
        else:
            break
    return n, ("inflow" if sign and sign > 0 else "outflow" if sign and sign < 0 else "flat")


def classify(total: float | None, lead: float | None) -> str | None:
    """Tag a same-signed window by the lead fund's share of it."""
    if not total or lead is None:
        return None
    share = lead / total
    if share < 0:
        return "lead opposing"
    if share >= OFFSETTING_MIN:
        return "offsetting"
    if share >= CONVICTION_MIN:
        return "conviction"
    return "broad"


def summarize(rows: list[dict]) -> dict:
    want = (*FUNDS, "Total")
    reported = [r for r in rows if any(r.get(k) not in (None, 0.0) for k in want)]
    complete = [r for r in reported if all(r.get(f) is not None for f in FUNDS)]

    partial = None
    if reported and any(reported[-1].get(f) is None for f in FUNDS):
        last = reported[-1]
        have = [f for f in FUNDS if last.get(f) is not None]
        partial = {
            "date": last["date"],
            "reported_total": round(sum(last[f] for f in have), 1),
            "other": _other(last),
            "reported": have,
            "pending": [f for f in FUNDS if last.get(f) is None],
        }

    windows = [_window(complete, w) for w in WINDOWS]
    primary = windows[0]
    streak_days, streak_sign = _streak(complete)

    latest = complete[-1] if complete else None
    age = None
    if latest:
        try:
            d = datetime.strptime(latest["date"], "%d %b %Y").date()
            age = (datetime.now(timezone.utc).date() - d).days
        except ValueError:
            pass

    return {
        "lead": LEAD,
        "as_of": latest["date"] if latest else None,
        "age_days": age,
        "latest_total": latest["Total"] if latest else None,
        "latest_lead": latest[LEAD] if latest else None,
        "days_complete": len(complete),
        "windows": windows,
        "streak_days": streak_days,
        "streak_sign": streak_sign,
        "regime": classify(primary["total"], primary["lead"]),
        "partial": partial,
    }


def _cache_file(cfg) -> Path:
    return Path(cfg.cache_dir) / CACHE_NAME


def collect(cfg) -> SourceResult:
    errors = []
    for url in (ALL_DATA_URL, NAV_URL):
        try:
            rows = parse_table(fetch_html(url, cfg.timeout))
            summary = summarize(rows)
            payload = {
                "source": url,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                **summary,
            }
            try:
                path = _cache_file(cfg)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload, indent=2))
            except Exception:
                pass  # a cache we can't write is not a reason to lose a good fetch
            return SourceResult(
                name=NAME, available=True, data=payload, as_of=summary["as_of"]
            )
        except Exception as e:
            errors.append(f"{url}: {e}")

    # Live fetch failed on every page. Serve the last good payload, flagged —
    # yesterday's finalized flows are far more useful than nothing, provided
    # the reader is told they're old.
    try:
        cached = json.loads(_cache_file(cfg).read_text())
    except Exception:
        return unavailable(NAME, "; ".join(errors))

    # age_days was computed when the cache was written and would understate how
    # old this is; re-derive it against today.
    if cached.get("as_of"):
        try:
            d = datetime.strptime(cached["as_of"], "%d %b %Y").date()
            cached["age_days"] = (datetime.now(timezone.utc).date() - d).days
        except ValueError:
            pass
    return SourceResult(
        name=NAME,
        available=True,
        data=cached,
        stale=True,
        error="; ".join(errors),
        as_of=cached.get("as_of"),
    )


def _m(v: float | None) -> str:
    if v is None:
        return "n/a"
    return f"{v/1000:+.2f}B" if abs(v) >= 1000 else f"{v:+.1f}M"


def render_lines(d: dict) -> list[str]:
    if not d.get("as_of"):
        return ["no fully-reported day available"]
    # age_days is None when the upstream date string didn't parse; show the
    # date alone rather than the string "None".
    age = f", {d['age_days']}d ago" if d.get("age_days") is not None else ""
    lead = d.get("lead") or LEAD
    out = [
        f"latest {_m(d.get('latest_total'))} total | {_m(d.get('latest_lead'))} {lead} "
        f"({d.get('as_of') or 'unknown date'}{age})"
    ]
    for w in d.get("windows") or []:
        if not isinstance(w, dict):
            continue
        if w.get("covered"):
            out.append(
                f"{fmt(w.get('days'))}d net {_m(w.get('total'))} total | "
                f"{_m(w.get('lead'))} {lead}"
            )
        else:
            out.append(
                f"{fmt(w.get('days'))}d net n/a "
                f"({fmt(w.get('days_available'), missing='?')}d available)"
            )
    tag = f" — {d['regime']}" if d.get("regime") else ""
    out.append(f"streak {fmt(d.get('streak_days'))}d {d.get('streak_sign') or 'n/a'}{tag}")
    p = d.get("partial")
    if isinstance(p, dict):
        reported = p.get("reported") or []
        pending = p.get("pending") or []
        out.append(
            f"partial {p.get('date') or 'today'}: {_m(p.get('reported_total'))} from "
            f"{len(reported)}/{len(FUNDS)} funds, pending "
            f"{', '.join(pending) or 'n/a'}"
        )
    return out


def context_lines(d: dict) -> list[str]:
    if not d.get("as_of"):
        return []
    lead = d.get("lead") or LEAD
    age = (
        f"{fmt(d.get('age_days'))}d ago, " if d.get("age_days") is not None else ""
    )
    out = [
        f"BTC ETF flows as of {d['as_of']} ({age}fully reported): "
        f"{_m(d.get('latest_total'))} total, {_m(d.get('latest_lead'))} {lead}"
    ]
    # Naming the weekday explicitly: without it the model tends to infer one
    # from the date and state it as fact.
    try:
        dt = datetime.strptime(d["as_of"], "%d %b %Y")
        out.append(f"That ETF date was a {dt.strftime('%A')}")
    except (TypeError, ValueError):
        pass
    for w in d.get("windows") or []:
        if not isinstance(w, dict):
            continue
        if w.get("covered"):
            out.append(
                f"BTC ETF {fmt(w.get('days'))}d net: {_m(w.get('total'))} total, "
                f"{_m(w.get('lead'))} {lead}"
            )
        else:
            out.append(
                f"BTC ETF {fmt(w.get('days'))}d net: not available — only "
                f"{fmt(w.get('days_available'), missing='an unknown number of')} "
                f"fully-reported days exist. Do not treat this as zero or as a "
                f"smaller window's figure."
            )
    out.append(
        f"BTC ETF streak: {fmt(d.get('streak_days'))} consecutive "
        f"{d.get('streak_sign') or 'same-sign'} days"
        + (f", classified {d['regime']} (by {lead} share of the 5d total)"
           if d.get("regime") else "")
    )
    p = d.get("partial")
    if isinstance(p, dict):
        out.append(
            f"BTC ETF partial day {p.get('date') or 'today'} is IN PROGRESS and "
            f"excluded from every figure above: {_m(p.get('reported_total'))} from "
            f"{', '.join(p.get('reported') or []) or 'no funds yet'}, still pending "
            f"{', '.join(p.get('pending') or []) or 'n/a'}. Its direction is not yet "
            f"settled."
        )
    return out
