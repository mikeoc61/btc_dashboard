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
TICK_OK = "\u2713"   # CHECK MARK
TICK_NO = "\u2717"   # BALLOT X

# Tab icon. Drawn rather than set as a glyph: a "B" exists in every font, but
# the Bitcoin sign U+20BF does not, and a tab showing a substitution box is
# worse than no icon. The two strokes are rectangles, so the mark renders
# identically everywhere.
#
# Solid fill because a tab icon sits on the browser's chrome, not the page —
# a background of its own is what keeps it legible in light and dark themes
# alike, without needing to detect either.
_FAVICON_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="13" fill="#f7931a"/>'
    '<rect x="25" y="8" width="5" height="48" fill="#fff"/>'
    '<rect x="36" y="8" width="5" height="48" fill="#fff"/>'
    '<text x="33" y="47" font-size="40" font-weight="700" text-anchor="middle"'
    ' font-family="Helvetica,Arial,sans-serif" fill="#fff">B</text>'
    "</svg>"
)


# The same mark as rectangles on a 32x32 grid, for the raster copy. Safari has
# never read SVG favicons from a data URI, so it falls back to its own
# generated letter tile and the icon appears not to change at all.
#
# (x0, y0, x1, y1) in pixels, exclusive of the right/bottom edge. The two long
# strokes run past the letter top and bottom, which is what makes a B a Bitcoin
# sign; everything else is the letter itself.
_MARK_RECTS = (
    (9, 6, 13, 26),     # spine
    (9, 6, 19, 9),      # top bar
    (9, 14, 18, 17),    # middle bar
    (9, 23, 20, 26),    # bottom bar
    (16, 6, 20, 17),    # upper bowl
    (17, 14, 21, 26),   # lower bowl
    (13, 2, 16, 30),    # crossing stroke, left
    (17, 2, 20, 30),    # crossing stroke, right
)
_ORANGE = (0xF7, 0x93, 0x1A)
_WHITE = (0xFF, 0xFF, 0xFF)
_ICON_PX = 32


def _favicon_png() -> bytes:
    """Rasterise the mark to a 32x32 PNG, using only the standard library.

    Generated rather than pasted in as a base64 blob so the icon stays
    reviewable: a blob is unreadable in a diff and cannot be checked against
    the SVG it is supposed to match.
    """
    import struct
    import zlib

    rows = []
    for y in range(_ICON_PX):
        row = bytearray([0])                      # PNG filter byte: none
        for x in range(_ICON_PX):
            on = any(x0 <= x < x1 and y0 <= y < y1
                     for x0, y0, x1, y1 in _MARK_RECTS)
            row += bytes(_WHITE if on else _ORANGE)
        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", _ICON_PX, _ICON_PX, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"".join(rows), 9))
        + chunk(b"IEND", b"")
    )


def _favicon_png_data_uri() -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(_favicon_png()).decode("ascii")


def _favicon_data_uri() -> str:
    """The icon as a base64 data URI.

    Base64 rather than a percent-encoded SVG for two reasons. It removes every
    quoting hazard — the markup contains both quote characters and a `#`, each
    of which has bitten this file before. And it keeps the SVG's xmlns, which
    is an http:// URL, out of the page as literal text, so "this page
    references nothing external" stays checkable by searching for a scheme.
    """
    import base64

    encoded = base64.b64encode(_FAVICON_SVG.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{encoded}"

CSS = """
:root {
  --bg:#0d1117; --card:#161b22; --line:#30363d; --text:#e6edf3;
  --muted:#a3aebb; --accent:#79c0ff; --up:#56d364; --down:#ff7b72;
  --warn:#e3b341;
  /* Text in a UI face and numbers in a monospace one. Monospace everywhere
     smears at small sizes, especially bold on a dark background, and only the
     figures actually need the fixed advance width for column alignment. */
  --ui:system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
  --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
}
@media (prefers-color-scheme: light) {
  :root {
    --bg:#f6f8fa; --card:#fff; --line:#d0d7de; --text:#1f2328;
    --muted:#5a6069; --accent:#0550ae; --up:#116329; --down:#a40e26;
    --warn:#7d4e00;
  }
}
* { box-sizing:border-box; }
body {
  margin:0; padding:1.1rem; background:var(--bg); color:var(--text);
  /* No px base: inherit the browser's own default size, so a reader who has
     already turned it up gets that, and zoom scales everything evenly. */
  font-family:var(--ui); font-size:1rem; line-height:1.5;
  -webkit-font-smoothing:antialiased; -moz-osx-font-smoothing:grayscale;
}
header { display:flex; flex-wrap:wrap; gap:.9rem; align-items:baseline;
         justify-content:space-between; border-bottom:1px solid var(--line);
         padding-bottom:.7rem; margin-bottom:1.1rem; }
h1 { font-size:1.05rem; margin:0; letter-spacing:.06em; color:var(--accent); }
.meta { color:var(--muted); font-size:.85rem; font-family:var(--mono); }
.ticks { display:flex; gap:1rem; font-size:.85rem; }
.tick .mark { font-weight:700; }
.tick.ok .mark { color:var(--up); }
.tick.no .mark { color:var(--down); }
.tick.no { color:var(--muted); }
.grid { display:grid; gap:.85rem;
        grid-template-columns:repeat(auto-fit,minmax(310px,1fr)); }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:.85rem 1rem; }
.card h2 { font-size:.85rem; margin:0 0 .7rem; letter-spacing:.05em;
           color:var(--accent); display:flex; justify-content:space-between;
           align-items:baseline; gap:.6rem; font-weight:600; }
.badge { font-size:.78rem; font-weight:400; color:var(--muted);
         white-space:nowrap; font-family:var(--mono); }
.badge.warn { color:var(--warn); }
.row { display:flex; justify-content:space-between; align-items:baseline;
       gap:1rem; padding:.28rem 0; }
.row + .row, .note + .row {
  border-top:1px solid color-mix(in srgb, var(--line) 45%, transparent); }
.label { color:var(--muted); font-size:.9rem; }
.value { font-family:var(--mono); font-size:1.02rem; font-variant-numeric:tabular-nums;
         white-space:nowrap; font-weight:600; letter-spacing:-.01em; }
.value.up { color:var(--up); } .value.down { color:var(--down); }
.value.warn { color:var(--warn); }
.note { color:var(--muted); font-size:.8rem; line-height:1.45;
        padding:0 0 .3rem; margin-top:-.1rem; }
/* Muted so a coloured note stays secondary to the value it sits under. */
.note.up { color:color-mix(in srgb, var(--up) 78%, var(--muted)); }
.note.down { color:color-mix(in srgb, var(--down) 78%, var(--muted)); }
.note.warn { color:color-mix(in srgb, var(--warn) 78%, var(--muted)); }
.err { color:var(--warn); font-size:.88rem; }
.card.wide { grid-column:1/-1; }
.notable { background:var(--card); border:1px solid var(--line);
           border-left:3px solid var(--warn); border-radius:8px;
           padding:.7rem 1rem; margin-bottom:.85rem; }
.notable h2 { font-size:.85rem; margin:0 0 .35rem; letter-spacing:.05em;
              color:var(--warn); font-weight:600; }
.notable ul { margin:0; padding-left:1.1rem; }
.notable li { padding:.1rem 0; }
.askform { display:flex; gap:.6rem; }
.askform input { flex:1; padding:.6rem .7rem; background:var(--bg);
                 color:var(--text); border:1px solid var(--line);
                 border-radius:6px; font:inherit; font-size:1rem; }
.askform button, .linkish { padding:.6rem 1.1rem; border:1px solid var(--line);
                 border-radius:6px; background:var(--accent); color:var(--bg);
                 font:inherit; font-size:.95rem; font-weight:600; cursor:pointer; }
.linkish { padding:.15rem .55rem; background:none; color:var(--muted);
           font-weight:400; font-size:.8rem; }
.answer { white-space:pre-wrap; margin:.5rem 0; line-height:1.6;
          font-size:.98rem; }
footer { margin-top:1.2rem; padding-top:.7rem; border-top:1px solid var(--line);
         color:var(--muted); font-size:.8rem; line-height:1.5; }
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
            ntone = f" {m.note_tone}" if m.note_tone in ("up", "down", "warn") else ""
            out.append(f'<div class="note{ntone}">{_esc(m.note)}</div>')
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


def _answer_card(answer: dict | None) -> str:
    """The analyst's reply, or the reason there isn't one."""
    if not answer:
        return ""
    q = _esc(answer.get("question") or "")
    if answer.get("error"):
        body = f'<div class="err">{_esc(answer["error"])}</div>'
        meta = ""
    else:
        # pre-wrap, so the model's paragraphs survive without letting its text
        # introduce markup — it is escaped like every other free string here.
        body = f'<div class="answer">{_esc(answer.get("text") or "")}</div>'
        meta = (
            f'<div class="note">{_esc(answer.get("provider"))}/'
            f'{_esc(answer.get("model"))} · {_esc(answer.get("input_tokens"))} in / '
            f'{_esc(answer.get("output_tokens"))} out</div>'
        )
    return (
        '<section class="card wide"><h2>ANALYSIS</h2>'
        f'<div class="note">{q}</div>{body}{meta}</section>'
    )


def _notable(snapshot: dict) -> list[str]:
    """Readings worth leading with, gathered from the sources themselves.

    Each source owns its own thresholds, because what counts as extreme is a
    property of the measure, not of the page. Availability and staleness are
    added here since they are facts about the snapshot rather than about any
    one source.

    Every entry is a stated reading with its window attached — never an
    interpretation. "30d volatility at the 1st percentile of 2y" is a fact;
    "compression, expect a large move" is a forecast, and volatility carries no
    direction. The reader draws the conclusion.
    """
    out: list[str] = []
    for name in snap.ordered_names(snapshot):
        block = snapshot["sources"][name]
        label = snap.TITLES.get(name, name).split(" (")[0]
        if not block.get("available"):
            out.append(f"{label.lower()} unavailable")
            continue
        if block.get("stale"):
            age = block.get("cache_age_seconds")
            out.append(
                f"{label.lower()} is stale"
                + (f" ({human_age(age)} old)" if age is not None else "")
            )
        mod = snap.module_for(name)
        if mod is None or not hasattr(mod, "notable"):
            continue
        try:
            out.extend(mod.notable(block["data"]) or [])
        except Exception:
            # A threshold check must never cost the page.
            continue
    return out


def render_html(snapshot: dict, *, title: str = "BTC DASHBOARD",
                refresh: int | None = REFRESH_SECONDS,
                ask: bool = False, answer: dict | None = None) -> str:
    """The page. `ask` adds the analyst box, which needs a server behind it."""
    generated = str(snapshot.get("generated_at", ""))[:19].replace("T", " ")

    # The tick glyph is markup, not a CSS ::before. Injected by stylesheet it
    # disappears whenever styling does, and "which sources are available" is
    # information, so it must not live in the presentation layer. CSS only
    # colours it. A literal also avoids CSS hex escapes, which have to survive
    # two layers of quoting to reach the browser intact — one rewrite turned
    # \2713 into an octal escape and shipped a superscript one.
    ticks = "".join(
        f'<span class="tick {"ok" if b.get("available") else "no"}">'
        f'<span class="mark">{TICK_OK if b.get("available") else TICK_NO}</span> '
        f'{_esc(snap.TITLES.get(n, n).split(" (")[0])}</span>'
        for n, b in ((n, snapshot["sources"][n]) for n in snap.ordered_names(snapshot))
    )

    # Cards are ordered by the priority each source declares, not by source
    # order — volatility comes from the warehouse but belongs beside price.
    # `seq` keeps ties in source order so the layout does not shuffle.
    ordered = []
    for seq, name in enumerate(snap.ordered_names(snapshot)):
        block = snapshot["sources"][name]
        if not block.get("available"):
            ordered.append((99, seq, 0, name, block, None))
            continue
        for i, panel in enumerate(_panels_for(name, block)):
            ordered.append((panel.priority, seq, i, name, block, panel))
    ordered.sort(key=lambda r: (r[0], r[1], r[2]))

    cards = []
    for _, _, i, name, block, panel in ordered:
        if panel is None:
            cards.append(
                f'<section class="card"><h2>{_esc(snap.TITLES.get(name, name.upper()))}'
                f'<span class="badge warn">unavailable</span></h2>'
                f'<div class="err">{_esc(block.get("error") or "no data")}</div></section>'
            )
            continue
        # Every card of a source carries the badge. One source can produce
        # several cards, the grid wraps them onto different rows, and a badge on
        # the first alone leaves the others looking undated.
        label, cls = _badge(block)
        badge = f'<span class="badge {cls}">{_esc(label)}</span>'
        # The failure reason stays on the source's first card: repeating one
        # error three times reads as three problems.
        err = (f'<div class="err">refresh failed: {_esc(block["error"])}</div>'
               if i == 0 and block.get("stale") and block.get("error") else "")
        cards.append(
            f'<section class="card"><h2>{_esc(panel.title)}{badge}</h2>'
            f'{_rows(panel.metrics)}{err}</section>'
        )

    ask_html = ""
    if ask:
        ask_html = (
            '<section class="card wide"><h2>ASK'
            '<form method="post" action="/refresh" style="margin:0">'
            '<button class="linkish" type="submit">refresh data</button>'
            '</form></h2>'
            '<form method="post" action="/ask" class="askform">'
            '<input name="q" autofocus autocomplete="off" '
            'placeholder="ask a question about this snapshot">'
            '<button type="submit">Ask</button></form>'
            '<div class="note">Sent to the configured provider using the key on '
            'this machine — each question costs money. The analyst sees the '
            'facts on this page, including which are cached or stale.</div>'
            '</section>'
        ) + _answer_card(answer)

    notable = _notable(snapshot)
    notable_html = ""
    if notable:
        # Absent entirely when nothing qualifies. A strip that always finds
        # something to say teaches the reader to stop looking at it.
        items = "".join(f"<li>{_esc(n)}</li>" for n in notable)
        notable_html = (
            f'<section class="notable"><h2>NOTABLE</h2><ul>{items}</ul></section>'
        )

    meta_refresh = (
        f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{meta_refresh}<title>{_esc(title)}</title>
<link rel="icon" type="image/png" sizes="32x32" href="{_favicon_png_data_uri()}">
<link rel="icon" type="image/svg+xml" href="{_favicon_data_uri()}">
<style>{CSS}</style></head>
<body>
<header>
  <h1>{_esc(title)}</h1>
  <div class="ticks">{ticks}</div>
  <div class="meta">{_esc(generated)} UTC</div>
</header>
{notable_html}\n<main class="grid">{"".join(cards)}{ask_html}</main>
<footer>Data: local node + DuckDB · price: CoinGecko · ETF: Farside.
Percentile windows and volatility annualisation are stated on each figure —
compare those, not bare levels, against any external source.</footer>
</body></html>
"""
