"""Render a snapshot as a self-contained HTML page.

A fourth consumer of the snapshot, beside the terminal renderer, the analyst's
context block, and the raw JSON. It collects nothing and fetches nothing: given
a snapshot it returns a string, so the same function serves a file written by a
timer and a response returned by a web process.

Self-contained on purpose — the CSS is inline and there are no external assets,
fonts or scripts. The page therefore works from `file://`, from a static
server, and over an SSH tunnel with no internet access at all.

**Every qualifier survives the move.** A layout with room for rows invites
dropping the window a percentile was ranked against, or the annualisation
behind a volatility figure, because the numbers look tidier without them. Those
are the difference between a figure that can be compared to someone else's and
one that silently can't, so each `Metric` carries its note and the note is
rendered.
"""
from __future__ import annotations

import html as _html

from . import snapshot as snap
from .render import human_age
from .sources import Metric, Panel

REFRESH_SECONDS = 60

CSS = """
:root {
  --bg:#0d1117; --card:#161b22; --line:#30363d; --text:#e6edf3;
  --muted:#8b949e; --accent:#58a6ff; --up:#3fb950; --down:#f85149;
  --warn:#d29922; --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,monospace;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg:#f6f8fa; --card:#fff; --line:#d0d7de; --text:#1f2328;
    --muted:#656d76; --accent:#0969da; --up:#1a7f37; --down:#cf222e;
    --warn:#9a6700;
  }
}
* { box-sizing:border-box; }
body { margin:0; padding:1rem; background:var(--bg); color:var(--text);
       font-family:var(--mono); font-size:14px; line-height:1.45; }
header { display:flex; flex-wrap:wrap; gap:.75rem; align-items:baseline;
         justify-content:space-between; border-bottom:1px solid var(--line);
         padding-bottom:.6rem; margin-bottom:1rem; }
h1 { font-size:1rem; margin:0; letter-spacing:.08em; color:var(--accent); }
.meta { color:var(--muted); font-size:.8rem; }
.ticks { display:flex; gap:.75rem; font-size:.8rem; }
.tick.ok::before { content:"\\2713 "; color:var(--up); }
.tick.no::before { content:"\\2717 "; color:var(--down); }
.tick.no { color:var(--muted); }
.grid { display:grid; gap:.75rem;
        grid-template-columns:repeat(auto-fit,minmax(290px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:6px;
        padding:.7rem .85rem; }
.card h2 { font-size:.78rem; margin:0 0 .55rem; letter-spacing:.06em;
           color:var(--accent); display:flex; justify-content:space-between;
           align-items:baseline; gap:.5rem; font-weight:600; }
.badge { font-size:.68rem; font-weight:400; color:var(--muted);
         white-space:nowrap; }
.badge.warn { color:var(--warn); }
.row { display:flex; justify-content:space-between; align-items:baseline;
       gap:.75rem; padding:.2rem 0; }
.row + .row { border-top:1px solid color-mix(in srgb, var(--line) 45%, transparent); }
.label { color:var(--muted); font-size:.8rem; }
.value { font-variant-numeric:tabular-nums; white-space:nowrap; font-weight:600; }
.value.up { color:var(--up); } .value.down { color:var(--down); }
.value.warn { color:var(--warn); }
.note { color:var(--muted); font-size:.7rem; padding:0 0 .25rem; margin-top:-.15rem;
        max-width:100%; }
.err { color:var(--warn); font-size:.78rem; }
footer { margin-top:1rem; padding-top:.6rem; border-top:1px solid var(--line);
         color:var(--muted); font-size:.72rem; }
"""


def _esc(text) -> str:
    """Escape for HTML.

    Not optional: a snapshot may be *ingested* from elsewhere, and its error
    strings are free text controlled by whoever produced it. The same
    untrusted-input rule that governs the LLM prompt governs the page.
    """
    return _html.escape(str(text), quote=True)


def _badge(block: dict) -> tuple[str, str]:
    """Freshness marker for a card, mirroring the terminal's flags."""
    if block.get("stale"):
        age = block.get("cache_age_seconds")
        return (f"STALE {human_age(age)}" if age is not None else "STALE"), "warn"
    if block.get("cached"):
        return f"cached {human_age(block.get('cache_age_seconds'))}", ""
    return "live", ""


def _rows(metrics: list[Metric]) -> str:
    out = []
    for m in metrics:
        tone = f" {m.tone}" if m.tone in ("up", "down", "warn") else ""
        out.append(
            f'<div class="row"><span class="label">{_esc(m.label)}</span>'
            f'<span class="value{tone}">{_esc(m.value)}</span></div>'
        )
        if m.note:
            out.append(f'<div class="note">{_esc(m.note)}</div>')
    return "".join(out)


def _panels_for(name: str, block: dict) -> list[Panel]:
    """A source's panels, falling back to its terminal lines.

    A source without `html_panels` — or one from an ingested snapshot this
    build doesn't know — still renders, as a single card of plain lines rather
    than vanishing.
    """
    title = snap.TITLES.get(name, name.upper())
    mod = snap.module_for(name)
    if mod is None:
        return [Panel(title, [Metric("", "no renderer in this build",
                                     note="see the JSON for the raw data")])]
    try:
        if hasattr(mod, "html_panels"):
            return mod.html_panels(block["data"]) or []
        return [Panel(title, [Metric("", line) for line in mod.render_lines(block["data"])])]
    except Exception as e:
        return [Panel(title, [Metric("render failed", type(e).__name__)])]


def render_html(snapshot: dict, *, title: str = "BTC DASHBOARD",
                refresh: int | None = REFRESH_SECONDS,
                ask: bool = False) -> str:
    """The page. `ask` adds the analyst box, which needs a server behind it."""
    generated = str(snapshot.get("generated_at", ""))[:19].replace("T", " ")

    ticks = "".join(
        f'<span class="tick {"ok" if b.get("available") else "no"}">'
        f'{_esc(snap.TITLES.get(n, n).split(" (")[0])}</span>'
        for n, b in ((n, snapshot["sources"][n]) for n in snap.ordered_names(snapshot))
    )

    cards = []
    for name in snap.ordered_names(snapshot):
        block = snapshot["sources"][name]
        if not block.get("available"):
            cards.append(
                f'<section class="card"><h2>{_esc(snap.TITLES.get(name, name.upper()))}'
                f'<span class="badge warn">unavailable</span></h2>'
                f'<div class="err">{_esc(block.get("error") or "no data")}</div></section>'
            )
            continue
        label, cls = _badge(block)
        for i, panel in enumerate(_panels_for(name, block)):
            # The freshness badge rides the first card of a source only; the
            # rest inherit it visually by sitting next to it.
            badge = (f'<span class="badge {cls}">{_esc(label)}</span>' if i == 0 else "")
            err = (f'<div class="err">refresh failed: {_esc(block["error"])}</div>'
                   if i == 0 and block.get("stale") and block.get("error") else "")
            cards.append(
                f'<section class="card"><h2>{_esc(panel.title)}{badge}</h2>'
                f'{_rows(panel.metrics)}{err}</section>'
            )

    ask_html = ""
    if ask:
        ask_html = (
            '<section class="card" style="grid-column:1/-1">'
            '<h2>ASK</h2>'
            '<form method="post" action="/ask">'
            '<input name="q" placeholder="ask a question about this snapshot" '
            'style="width:100%;padding:.5rem;background:var(--bg);color:var(--text);'
            'border:1px solid var(--line);border-radius:4px;font:inherit">'
            '</form>'
            '<div class="note">Sent to the configured provider using the key on '
            'this machine. Costs money per question.</div></section>'
        )

    meta_refresh = (
        f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{meta_refresh}<title>{_esc(title)}</title><style>{CSS}</style></head>
<body>
<header>
  <h1>{_esc(title)}</h1>
  <div class="ticks">{ticks}</div>
  <div class="meta">{_esc(generated)} UTC</div>
</header>
<main class="grid">{"".join(cards)}{ask_html}</main>
<footer>Data: local node + DuckDB · price: CoinGecko · ETF: Farside.
Percentile windows and volatility annualisation are stated on each figure —
compare those, not bare levels, against any external source.</footer>
</body></html>
"""
