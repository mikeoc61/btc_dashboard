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
from datetime import date, datetime, timezone
from pathlib import Path

from . import Metric, Panel, SourceResult, fmt, unavailable

NAME = "flows"

NAV_URL = "https://farside.co.uk/btc/"
ALL_DATA_URL = "https://farside.co.uk/bitcoin-etf-flow-all-data/"
LEAD = "IBIT"
FUNDS = ("IBIT", "FBTC", "ARKB", "GBTC")
WINDOWS = (5, 20, 60)

# Farside publishes a trading day's flows once, in the evening — so a scrape
# more than once an hour buys nothing and only adds load to someone else's
# site. The cache layer also provides the stale-fallback when the site is
# unreachable, which is why this module no longer keeps its own cache file.
CACHE_TTL = 3600

DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}\s+\d{4}$")

# Flow dates are U.S. market trading days, so age is measured against the
# market's own calendar rather than UTC. UTC runs 4-5 hours ahead of New York,
# so a UTC anchor reports yesterday's flows as "2d ago" for the first hours of
# every UTC day — and for a reader west of Greenwich that window covers a large
# part of their afternoon. The market clock is also the right answer
# independent of where the reader sits: a viewer in London and one in Hawaii
# should agree on how old a trading day is.
MARKET_TZ = "America/New_York"

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


def _market_today() -> date:
    """Today on the U.S. market calendar.

    Falls back to UTC if the zone is unavailable (a container with no tzdata),
    which reproduces the old off-by-one rather than crashing — wrong by a day
    beats no flow data at all.
    """
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo(MARKET_TZ)).date()
    except Exception:
        return datetime.now(timezone.utc).date()


def age_days(date_str: str | None) -> int | None:
    """Whole days between a `D MMM YYYY` flow date and today, market calendar."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(date_str, "%d %b %Y").date()
    except (TypeError, ValueError):
        return None
    return (_market_today() - d).days


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


def lead_share(total: float | None, lead: float | None) -> float | None:
    """Lead fund's flow as a proportion of the window total."""
    if not total or lead is None:
        return None
    return lead / total


def classify(total: float | None, lead: float | None) -> str | None:
    """Tag a window by the lead fund's share of it, and by its direction.

    The direction word is not decoration. "conviction" alone reads as
    conviction *buying* in English, so an outflow window tagged with the bare
    word reads as the opposite of what it says — and this tag sits next to a
    streak that can point the other way, since a one-day inflow inside a
    five-day net outflow is perfectly ordinary.
    """
    share = lead_share(total, lead)
    if share is None:
        return None
    direction = "accumulation" if total > 0 else "distribution"
    if share < 0:
        # The lead moved against the window's net direction, so neither
        # "conviction" nor "broad" describes it.
        return f"{direction} against {LEAD}"
    if share >= OFFSETTING_MIN:
        return f"offsetting {direction}"
    if share >= CONVICTION_MIN:
        return f"conviction {direction}"
    return f"broad {direction}"


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

    return {
        "lead": LEAD,
        "as_of": latest["date"] if latest else None,
        "age_days": age_days(latest["date"]) if latest else None,
        "latest_total": latest["Total"] if latest else None,
        "latest_lead": latest[LEAD] if latest else None,
        "days_complete": len(complete),
        "windows": windows,
        "streak_days": streak_days,
        "streak_sign": streak_sign,
        "regime": classify(primary["total"], primary["lead"]),
        # Which window the regime describes, and the share behind it. Without
        # these the tag is a bare adjective with no stated subject — which is
        # how it ended up being read as a property of the streak.
        "regime_window_days": primary["days"],
        "lead_share_pct": (
            round(lead_share(primary["total"], primary["lead"]) * 100, 1)
            if lead_share(primary["total"], primary["lead"]) is not None
            else None
        ),
        "partial": partial,
    }


def collect(cfg) -> SourceResult:
    """Scrape and summarize the flow table.

    Caching — both the within-TTL read path and the stale-on-failure fallback —
    is handled by `btc_dashboard.cache`, not here. This function's only job is
    to return live data or say why it couldn't.
    """
    errors = []
    for url in (ALL_DATA_URL, NAV_URL):
        try:
            rows = parse_table(fetch_html(url, cfg.timeout))
            summary = summarize(rows)
            return SourceResult(
                name=NAME,
                available=True,
                data={
                    "source": url,
                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                    **summary,
                },
                as_of=summary["as_of"],
            )
        except Exception as e:
            errors.append(f"{url}: {e}")
    return unavailable(NAME, "; ".join(errors))


def refresh_derived(data: dict) -> dict:
    """Recompute `age_days` against today's market date.

    Called by the cache layer whenever this payload is served from disk. Age is
    relative to when it is *read*, not when it was fetched — without this, a
    cache served on the stale-fallback path would report a three-day-old
    trading day as "1d ago", which is precisely when the reader needs it least.
    """
    if data.get("as_of"):
        data["age_days"] = age_days(data["as_of"])
    return data


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
        if not w.get("covered"):
            out.append(
                f"{fmt(w.get('days'))}d net n/a "
                f"({fmt(w.get('days_available'), missing='?')}d available)"
            )
            continue
        line = (
            f"{fmt(w.get('days'))}d net {_m(w.get('total'))} total | "
            f"{_m(w.get('lead'))} {lead}"
        )
        # The regime tag belongs on the window it is computed from, where the
        # total's sign is visible beside it. On the streak line it described a
        # different measure, which could and did point the other way.
        if w.get("days") == d.get("regime_window_days") and d.get("regime"):
            share = d.get("lead_share_pct")
            share_txt = f"{fmt(share, '.0f')}% {lead} — " if share is not None else ""
            line += f" ({share_txt}{d['regime']})"
        out.append(line)

    out.append(f"streak {fmt(d.get('streak_days'))}d {d.get('streak_sign') or 'n/a'}")
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
        f"{d.get('streak_sign') or 'same-sign'} days. This counts only the most "
        f"recent run and says nothing about the size of the flows in it."
    )
    if d.get("regime"):
        window = fmt(d.get("regime_window_days"))
        share = d.get("lead_share_pct")
        out.append(
            f"BTC ETF regime over the {window}d window: {d['regime']} "
            + (f"({lead} is {fmt(share, '.0f')}% of that window's net). "
               if share is not None else "")
            + f"This describes the {window}d window ONLY — it is not a property "
            f"of the streak above, and the two can point in opposite directions. "
            f"A short inflow run inside a longer net outflow is ordinary and is "
            f"not by itself evidence of a turn."
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


def html_panels(d: dict) -> list[Panel]:
    lead = d.get("lead") or LEAD
    if not d.get("as_of"):
        return [Panel("ETF FLOWS (US SPOT)",
                      [Metric("Status", "no fully-reported day yet")])]

    age = (f" · {fmt(d.get('age_days'))}d ago" if d.get("age_days") is not None else "")
    rows = [Metric("Latest", _m(d.get("latest_total")),
                   note=f"{_m(d.get('latest_lead'))} {lead} · {d['as_of']}{age}",
                   tone=_tone(d.get("latest_total")))]

    for w in d.get("windows") or []:
        if not isinstance(w, dict):
            continue
        days = fmt(w.get("days"))
        if not w.get("covered"):
            rows.append(Metric(f"{days}D Net", "n/a",
                               note=f"only {fmt(w.get('days_available'), missing='?')} "
                                    f"fully-reported days — not zero"))
            continue
        note = f"{_m(w.get('lead'))} {lead}"
        if w.get("days") == d.get("regime_window_days") and d.get("regime"):
            share = d.get("lead_share_pct")
            note += (f" ({fmt(share, '.0f')}% of it) · {d['regime']}"
                     if share is not None else f" · {d['regime']}")
        rows.append(Metric(f"{days}D Net", _m(w.get("total")), note=note,
                           tone=_tone(w.get("total"))))

    rows.append(Metric(
        "Streak", f"{fmt(d.get('streak_days'))}d {d.get('streak_sign') or 'n/a'}",
        note="most recent run only — says nothing about size, and can point "
             "the other way from the window above"))

    p = d.get("partial")
    if isinstance(p, dict):
        rows.append(Metric(
            "In Progress", _m(p.get("reported_total")),
            note=f"{p.get('date') or 'today'} · "
                 f"{len(p.get('reported') or [])}/{len(FUNDS)} funds in, pending "
                 f"{', '.join(p.get('pending') or []) or 'n/a'} — excluded above",
            tone="warn"))
    return [Panel("ETF FLOWS (US SPOT)", rows)]


def _tone(v) -> str | None:
    if not isinstance(v, (int, float)):
        return None
    return "up" if v > 0 else ("down" if v < 0 else None)
