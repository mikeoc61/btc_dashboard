"""U.S. spot Bitcoin ETF net flows, scraped from Farside Investors.

Values are US$ millions; negative is net outflow.

Three properties here are not obvious and are the difference between a number
that means something and one that doesn't:

1. **A reported zero is not a missing cell.** Farside renders an unpublished
   figure as `-` or blank and a genuine zero flow as `0.0`. Collapsing the two
   turns "hasn't reported" into "reported no flow", which silently drags every
   average toward zero. Blank parses to None; only an explicit 0 is 0.0.

2. **A day counts only once every tracked fund has reported and Farside has
   published a Total for it.** Funds post progressively through the afternoon,
   so a day read mid-session has a real but incomplete Total whose *sign* can
   still flip. Partial days are excluded from every latest/streak/window figure
   and surfaced separately. The Total is required for the same reason it is the
   basis of every figure here: a day without one cannot contribute to a sum
   taken over every listed fund, and counting it as complete puts a day into a
   window that adds nothing to its total.

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

from . import Metric, Panel, SourceResult, fmt, safe_text, unavailable

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
            # Farside opens the body with a stray `<tr>` that never closes, so
            # html.parser nests every real row inside it. That wrapper has no
            # cells of its own, but `find_all` descends, so it collects the
            # nested rows' cells and its leading ones alias the first data
            # row — emitting the launch day twice. Skip any row containing a
            # row: a cell of a table is never another table's row.
            for tr in table.find_all("tr") if tr.find("tr") is None
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
    # Summed unguarded on purpose. `summarize` admits a row into `complete`
    # only once its Total and all four funds are present, so there is nothing
    # here to skip — and a guard that skips one silently turns a broken
    # invariant into a window that is short by a day and says it is covered.
    # If that invariant ever breaks, `collect` catches the TypeError and the
    # block reports why it is missing, which beats a quietly wrong figure.
    return {
        "days": days,
        "days_available": len(recent),
        "covered": covered,
        "total": round(sum(r["Total"] for r in recent), 1) if covered else None,
        "lead": round(sum(r[LEAD] for r in recent), 1) if covered else None,
    }


def _streak(complete: list[dict]) -> tuple[int, str]:
    """Consecutive same-sign days, walking back through the usable ones.

    Walks `complete`, so it counts *across* the days excluded from it — a
    market closure, a day still reporting, a day with no published Total. That
    is the same rule the windows use, and the alternative reads worse: a
    fourteen-day outflow run interrupted by one unmeasurable day is a
    fourteen-day run, not a three-day one.
    """
    sign = None
    n = 0
    for r in reversed(complete):
        # Unguarded: `complete` has no missing Total. See `_window`.
        v = r["Total"]
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


def _carries_flows(row: dict) -> bool:
    """Whether this row is a day of data at all.

    Two shapes are not. One is a date with nothing published against it. The
    other is a date where no tracked fund has posted and the site has printed
    `0.0` in the `Total` column — which happens for a U.S. market holiday, and
    equally for an ordinary day whose flows have not been published yet. The
    row cannot tell you which, and it does not matter: neither is a day that
    reported no flow, and averaging either in drags every figure toward zero.

    Of the 17 rows carrying that shape in the BTC history on 3 Sep 2026, 16 are
    market holidays — MLK, Presidents' Day, Good Friday, Memorial Day,
    Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas, New Year,
    and the Jan 2025 day of mourning — and the 17th is that day's own row,
    still unpublished at the time of reading. No holiday row appears after
    19 Jun 2025 despite the holidays since, so the site seems to have stopped
    listing them; the shape itself recurs daily regardless.

    The test is *no fund posted*, never *everything is zero*. That distinction
    is the whole point, and the looser version is a live bug on any asset whose
    flows are small: Farside rounds to 0.1M, so a quiet session prints `0.0` in
    every column while being a perfectly real trading day. Across 542 ETH rows
    there are 12 such sessions, 5 Nov 2024 — US election day — among them. BTC
    has none, which is a fact about the size of its flows rather than about the
    data, and is exactly the sort of thing that stops being true when someone
    adds an asset.
    """
    want = (*FUNDS, "Total")
    if not any(row.get(k) is not None for k in want):
        return False
    return not (all(row.get(f) is None for f in FUNDS) and row.get("Total") == 0.0)


def summarize(rows: list[dict]) -> dict:
    # A fund column holding an explicit `0.0` is a day that reported no flow,
    # and docstring point 1 says it survives. See `_carries_flows` for the two
    # shapes that do not.
    reported = [r for r in rows if _carries_flows(r)]
    # Both halves are load-bearing. Without the funds, a day read mid-session
    # enters the windows with a Total whose sign can still flip. Without the
    # Total, it enters them contributing nothing — a five-day window summing
    # four days, reported as covered, which is the short-sum-wearing-a-longer-
    # label defect this module exists to avoid.
    complete = [
        r for r in reported
        if r.get("Total") is not None and all(r.get(f) is not None for f in FUNDS)
    ]

    partial = None
    if reported and any(reported[-1].get(f) is None for f in FUNDS):
        last = reported[-1]
        have = [f for f in FUNDS if last.get(f) is not None]
        partial = {
            "date": last["date"],
            # Three numbers, because two of them are on different bases and
            # the panel's other figures use only one of them.
            #
            # `published_total` is Farside's own Total for the row: everything
            # published for that day so far, tracked funds and untracked alike.
            # Every other figure here — latest, the windows, the streak — is on
            # that basis, so the headline has to be too or it is not comparable
            # with the numbers it sits under.
            #
            # `reported_total` sums only the tracked funds that have reported.
            # It was the headline once, and on 27 Aug 2026 it read -81.1M
            # against a published -35.3M: 56% of the magnitude sitting in the
            # untracked remainder, which on another day is enough to flip the
            # sign. A partial day whose sign can flip is the exact hazard this
            # module excludes partial days from the windows to avoid.
            "published_total": last.get("Total"),
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
    age = f", {fmt(d['age_days'])}d ago" if d.get("age_days") is not None else ""
    # The fund name, the date and the regime tag below are the snapshot's text,
    # not this module's wording, so they are bounded before they reach a line.
    # An ingested payload owns all three, and a newline in any of them starts a
    # line at column 0, where the panel's body indent no longer applies.
    lead = safe_text(d.get("lead") or LEAD)
    as_of = safe_text(d.get("as_of") or "unknown date")
    out = [
        f"latest {_m(d.get('latest_total'))} total | {_m(d.get('latest_lead'))} {lead} "
        f"({as_of}{age})"
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
            line += f" ({share_txt}{safe_text(d['regime'])})"
        out.append(line)

    sign = safe_text(d.get("streak_sign") or "n/a")
    out.append(f"streak {fmt(d.get('streak_days'))}d {sign}")
    p = d.get("partial")
    if isinstance(p, dict):
        reported = p.get("reported") or []
        pending = p.get("pending") or []
        value, basis = _partial_headline(p)
        split = _partial_split(p)
        day = safe_text(p.get("date") or "today")
        still = ", ".join(safe_text(f) for f in pending) or "n/a"
        out.append(
            f"partial {day}: {value} {basis}"
            + (f" ({split})" if split else "")
            + f", {len(reported)}/{len(FUNDS)} tracked funds in, pending "
            + still
        )
    return out


def context_lines(d: dict) -> list[str]:
    if not d.get("as_of"):
        return []
    lead = safe_text(d.get("lead") or LEAD)
    as_of = safe_text(d["as_of"])
    age = (
        f"{fmt(d.get('age_days'))}d ago, " if d.get("age_days") is not None else ""
    )
    out = [
        # Scope stated before any figure. These are U.S. spot ETFs only — not
        # futures products, not non-U.S. listings, and not a measure of global
        # capital flow. A model given "ETF flows" without that will generalise.
        f"BTC ETF flows below cover U.S. spot ETFs only ({', '.join(FUNDS)} and "
        f"the untracked remainder). They are one channel of demand, not total "
        f"market flow.",
        f"BTC ETF flows as of {as_of} ({age}fully reported): "
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

    sign = safe_text(d.get("streak_sign") or "same-sign")
    out.append(
        f"BTC ETF streak: {fmt(d.get('streak_days'))} consecutive "
        f"{sign} days. This counts only the most "
        f"recent run and says nothing about the size of the flows in it."
    )
    if d.get("regime"):
        window = fmt(d.get("regime_window_days"))
        share = d.get("lead_share_pct")
        out.append(
            f"BTC ETF regime over the {window}d window: {safe_text(d['regime'])} "
            + (f"({lead} is {fmt(share, '.0f')}% of that window's net). "
               if share is not None else "")
            + f"This describes the {window}d window ONLY — it is not a property "
            f"of the streak above, and the two can point in opposite directions. "
            f"A short inflow run inside a longer net outflow is ordinary and is "
            f"not by itself evidence of a turn."
        )
    p = d.get("partial")
    if isinstance(p, dict):
        value, basis = _partial_headline(p)
        split = _partial_split(p)
        day = safe_text(p.get("date") or "today")
        so_far = ", ".join(safe_text(f) for f in p.get("reported") or [])
        still = ", ".join(safe_text(f) for f in p.get("pending") or [])
        out.append(
            f"BTC ETF partial day {day} is IN PROGRESS and "
            f"excluded from every figure above: {value} {basis}"
            + (f" ({split})" if split else "")
            + f". Reported so far: {so_far or 'no tracked funds yet'}, still "
            f"pending {still or 'n/a'}. This is on the "
            f"same basis as the figures above — every published fund, not only the "
            f"itemized ones. Its direction is not yet settled."
        )
    return out


def html_panels(d: dict) -> list[Panel]:
    lead = d.get("lead") or LEAD
    if not d.get("as_of"):
        return [Panel("ETF FLOWS (US SPOT)", priority=60,
                      metrics=[Metric("Status", "no fully-reported day yet")])]

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
        # Both halves earn their place: the streak counts days, not dollars, and
        # it routinely disagrees with the windows beside it — an 8d inflow run
        # sat next to a 60d net of -571.1M the day this was shortened. Trimmed,
        # not dropped; the regime tag was moved off this measure once already
        # for pointing the other way.
        note="run length, not size — can point opposite the windows above"))

    p = d.get("partial")
    if isinstance(p, dict):
        value, basis = _partial_headline(p)
        split = _partial_split(p)
        rows.append(Metric(
            "In Progress", value,
            note=f"{p.get('date') or 'today'} · {basis}"
                 + (f" · {split}" if split else "")
                 + f" · {len(p.get('reported') or [])}/{len(FUNDS)} tracked funds in, "
                 f"pending {', '.join(p.get('pending') or []) or 'n/a'} — excluded above",
            tone="warn"))
    return [Panel("ETF FLOWS (US SPOT)", rows, priority=60)]


def _partial_split(p: dict) -> str:
    """How a partial day's published figure divides into tracked and untracked.

    Stated wherever the figure is, because the headline is on Farside's Total
    basis and the funds this module itemizes are only part of it. Without the
    split the reader cannot reconcile the number to the per-fund rows above it,
    and the gap invites being read as a pending fund's value — which it is not.
    """
    tracked, other = p.get("reported_total"), p.get("other")
    if p.get("published_total") is None or other is None:
        return ""
    return f"tracked {_m(tracked)} + untracked {_m(other)}"


def _partial_headline(p: dict) -> tuple[str, str]:
    """The partial figure and what it is measured over.

    Falls back to the tracked-only sum when Farside published no Total for the
    row — labelled as such rather than quietly changing basis, since a number
    on a different basis wearing the same label is the defect this replaced.
    """
    if p.get("published_total") is not None:
        return _m(p.get("published_total")), "published so far"
    return _m(p.get("reported_total")), f"{', '.join(FUNDS)} only, no published total"


def _tone(v) -> str | None:
    if not isinstance(v, (int, float)):
        return None
    return "up" if v > 0 else ("down" if v < 0 else None)


# A run this long is uncommon enough to mention. Deliberately not a size
# threshold: the streak counts days, and mixing a duration test with a
# magnitude one would report two different things under one heading.
NOTABLE_STREAK_DAYS = 5


def notable(d: dict) -> list[str]:
    days, sign = d.get("streak_days"), d.get("streak_sign")
    if isinstance(days, int) and days >= NOTABLE_STREAK_DAYS and sign in ("inflow", "outflow"):
        return [f"ETF flows: {days} consecutive {sign} days"]
    return []
