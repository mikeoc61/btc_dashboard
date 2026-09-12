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
import json

from . import composite, snapshot as snap
from .render import human_age
from .sources import Metric, Panel

REFRESH_SECONDS = 60

# The regions that carry the data, named once because three things have to
# agree on them: the page that lays them out, the fragment that re-renders
# them, and the script that patches one into the other. Three literals would
# drift the first time a region moved.
LIVE_IDS = ("ticks", "stamp", "composite", "cards")

# The regions the PNG capture draws, named for the same reason `LIVE_IDS` is:
# what the image contains has to be agreed on by the markup that ids the
# regions and the script that clones them.
#
# An allow-list, and deliberately so. Its failure mode is a region added to the
# page later being absent from the image, which anyone looking at the image can
# see. The alternative — clone `<main>` and strip the ask box out — fails the
# other way, silently *including* the ask box the first time that class is
# renamed, and a leaked half-typed question is only noticed after the image has
# been shared.
#
# The footer is in it on purpose. It carries the provenance and the "compare
# the stated windows, not bare levels" line, and a PNG is the copy most likely
# to be read away from this page — so dropping the qualifier from exactly the
# copy that travels is the regression this project keeps having.
#
# The notable readings are not named here because they are no longer a region:
# they ride in the balance card's own heading, inside `composite`.
CAPTURE_IDS = ("pagehead", "composite", "cards", "pagefoot")

# Patch those regions on a timer instead of reloading the document.
#
# A meta refresh replaces the page, and with it whatever is half-typed in the
# ask box — the one thing on the page the reader owns rather than the snapshot.
# So the data updates in place and the ask box is never touched; it changes
# only when an answer comes back from a POST.
#
# Contents are swapped, not the elements: the wrappers hold the layout — the
# header's flex row, and the slot the notable strip occupies on the ticks
# where nothing qualifies — so they stay put and only what they say changes.
#
# A failed fetch is swallowed and the last good render stays on screen. The
# stamp then visibly stops advancing, which is the honest signal that updates
# have stopped; a spinner or a retry storm would be worse than the silence.
_UPDATER_JS = """<script>
(function () {
  var url = URL_JSON, ids = IDS_JSON;
  function tick() {
    fetch(url, {cache: "no-store"})
      .then(function (r) { if (!r.ok) throw new Error(r.status); return r.text(); })
      .then(function (text) {
        var doc = new DOMParser().parseFromString(text, "text/html");
        ids.forEach(function (id) {
          var from = doc.getElementById(id), to = document.getElementById(id);
          if (from && to) to.innerHTML = from.innerHTML;
        });
      })
      .catch(function () {});
  }
  setInterval(tick, EVERY_MS);
})();
</script>"""

# Device pixels per CSS pixel in the capture. 2 is retina-crisp and costs about
# 290KB for a page of six cards; 1 is legibly soft on any modern display.
CAPTURE_SCALE = 2

# Draw the data regions to a PNG, in the browser, with no server involved.
#
# The page is cloned into an `<svg><foreignObject>`, that SVG is decoded as an
# image, and the image is drawn to a canvas. The whole reason this is possible
# in ~80 lines is that the page is already self-contained: the stylesheet is
# inline and there are no external fonts or assets, so nothing has to be
# fetched and inlined first, and nothing can taint the canvas.
#
# Three things here look like they could be simplified and cannot. Each was
# established by trying the tidier version and watching it fail:
#
# 1. The SVG is delivered as a `data:` URL, never a `blob:`. Both decode, but
#    an SVG from a blob: URL taints the canvas and every export then throws
#    SecurityError. The blob form is the shorter code and it is the broken one.
# 2. `body`'s own declarations are copied onto the clone's wrapper. A
#    foreignObject holds a bare `<div>`, so `body { color; font-family;
#    font-size; padding }` matches nothing inside the image and that text falls
#    back to the initial values \u2014 black, serif, 16px. The failure is partial
#    and plausible: rows carrying their own colour still look right while
#    everything inheriting from body goes dark, which reads as a contrast bug
#    rather than as a missing rule.
# 3. The clone is serialised with XMLSerializer, not read off innerHTML. A
#    foreignObject's contents are parsed as XML, where `&nbsp;` is an undefined
#    entity \u2014 the separator in the NOTABLE strip would stop the parse dead.
#    Serialising the live DOM emits the character itself.
#
# Failure is reported in the page rather than swallowed. Unlike the updater
# above, where silence leaves the last good render on screen, a capture that
# fails quietly hands over a blank or half-drawn PNG that looks like the
# dashboard until someone reads it.
_CAPTURE_JS = """<script>
(function () {
  var IDS = IDS_JSON, SCALE = SCALE_N;
  var note = document.getElementById("capnote");
  function say(m) { if (note) note.textContent = m || ""; }

  // Names read from the stylesheet's own `:root` rule rather than listed here,
  // so a token added to the CSS cannot leave the image resolving it to
  // nothing. Values are read computed, which is what settles light against
  // dark: an SVG image renders in its own sandbox, and inheriting the reader's
  // prefers-color-scheme through it is not something to rely on.
  function pinnedTokens() {
    var root = getComputedStyle(document.documentElement), out = "";
    var sheets = document.styleSheets;
    for (var s = 0; s < sheets.length; s++) {
      var rules = sheets[s].cssRules;
      for (var i = 0; i < rules.length; i++) {
        if (rules[i].selectorText !== ":root") continue;
        for (var j = 0; j < rules[i].style.length; j++) {
          var name = rules[i].style[j];
          if (name.slice(0, 2) === "--")
            out += name + ":" + root.getPropertyValue(name).trim() + ";";
        }
      }
    }
    return ":root{" + out + "}";
  }

  var CARRY = ["color", "font-family", "font-size", "line-height", "padding",
               "-webkit-font-smoothing"];

  function scene() {
    var body = getComputedStyle(document.body), carried = "";
    for (var i = 0; i < CARRY.length; i++)
      carried += CARRY[i] + ":" + body.getPropertyValue(CARRY[i]) + ";";

    var width = document.body.clientWidth;
    var stage = document.createElement("div");
    // Laid out off-screen rather than hidden: the height has to be measured
    // from a real layout, and `display:none` has no height to measure.
    stage.setAttribute("style", "position:absolute;left:-99999px;top:0;" +
      "box-sizing:border-box;width:" + width + "px;background:" +
      body.backgroundColor + ";" + carried);
    IDS.forEach(function (id) {
      var n = document.getElementById(id);
      if (n) stage.appendChild(n.cloneNode(true));
    });

    document.body.appendChild(stage);
    var height = Math.ceil(stage.getBoundingClientRect().height);
    var markup = new XMLSerializer().serializeToString(stage)
                   .replace("position:absolute;left:-99999px;top:0;", "");
    document.body.removeChild(stage);

    // Every stylesheet, not the first: the image has to carry whatever the
    // page is actually wearing, and a second block added later would otherwise
    // be silently missing from it.
    var sheets = document.querySelectorAll("style"), style = "";
    for (var s = 0; s < sheets.length; s++) style += sheets[s].textContent;
    style += pinnedTokens();
    return {
      width: width, height: height, background: body.backgroundColor,
      svg: '<svg xmlns="http://www.w3.org/2000/svg" width="' + width +
           '" height="' + height + '"><foreignObject x="0" y="0" width="' +
           width + '" height="' + height + '">' +
           '<div xmlns="http://www.w3.org/1999/xhtml"><style>' + style +
           "</style>" + markup + "</div></foreignObject></svg>"
    };
  }

  function png(view) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.onload = function () {
        try {
          var c = document.createElement("canvas");
          c.width = Math.round(view.width * SCALE);
          c.height = Math.round(view.height * SCALE);
          var ctx = c.getContext("2d");
          // The canvas starts transparent and the page's background lives on
          // `body`, which is not in the capture. Without this the PNG is
          // transparent behind the cards and unreadable on a dark backdrop.
          ctx.fillStyle = view.background;
          ctx.fillRect(0, 0, c.width, c.height);
          ctx.drawImage(img, 0, 0, c.width, c.height);
          c.toBlob(function (blob) {
            blob ? resolve(blob) : reject(new Error("the canvas produced no image"));
          }, "image/png");
        } catch (e) { reject(e); }
      };
      img.onerror = function () {
        reject(new Error("this browser would not render the page as an image"));
      };
      img.src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(view.svg);
    });
  }

  // Named from the stamp the image itself carries, so the file and the figures
  // in it cannot disagree. Read at click time, not at page load: the stamp
  // advances on every tick.
  function filename() {
    var stamp = document.getElementById("stamp");
    var digits = (stamp ? stamp.textContent : "").replace(/[^0-9]/g, "");
    return "btc-dashboard-" + (digits.slice(0, 14) || "snapshot") + ".png";
  }

  function fail(e) { say("capture failed: " + e.message); }

  function save() {
    say("rendering\\u2026");
    png(scene()).then(function (blob) {
      var a = document.createElement("a");
      var url = URL.createObjectURL(blob);
      a.href = url;
      a.download = filename();
      a.click();
      // Not revoked immediately: taking the URL back before the browser has
      // finished with it cancels the download.
      setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
      say("saved " + a.download);
    }).catch(fail);
  }

  function copy() {
    if (!navigator.clipboard || !window.ClipboardItem) {
      say("clipboard needs a secure context \\u2014 use save instead");
      return;
    }
    say("rendering\\u2026");
    // The ClipboardItem is handed the *promise*, not an awaited blob. The
    // rendering is asynchronous, and a write issued after the click has been
    // dispatched is no longer inside the user gesture the browser requires.
    var item = new ClipboardItem({"image/png": png(scene())});
    navigator.clipboard.write([item])
      .then(function () { say("copied to the clipboard"); })
      .catch(fail);
  }

  var actions = {copy: copy, save: save};
  var buttons = document.querySelectorAll("[data-capture]");
  for (var i = 0; i < buttons.length; i++) {
    buttons[i].addEventListener("click", function (e) {
      actions[e.currentTarget.getAttribute("data-capture")]();
    });
  }
})();
</script>"""
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
/* A breakpoint, not a card width. auto-fit stretches tracks to fill the row, so
   a rendered card is always wider than this number and never equal to it: at
   1920, the width this is tuned for, three columns come out at 619px. It does
   not scale a card either -- changing it changes the column count, and the
   width jumps in steps.

   Sized to hold exactly three, because there are six cards and 3x2 packs with
   nothing left over. Four columns was measurably worse on both counts: two
   empty slots, AND a taller page (939px against 874px), because narrower cards
   wrap more notes onto a second line. Fewer, wider columns making the page
   shorter is the counterintuitive part, and it is why this is tuned by
   measurement rather than by arithmetic on this number.

   The cost is narrow windows: at a viewport around 1500 and narrower it falls
   to two columns, where 372 held three down to about 1178. At 1367 that is a
   1210px page against roughly 1100 before. Retune against a real
   viewport, and beware a zoomed-out browser -- it reports a viewport the reader
   does not actually use. */
.grid { display:grid; gap:.85rem;
        grid-template-columns:repeat(auto-fit,minmax(480px,1fr)); }
/* Vertical rhythm is tight on purpose. Every card is a stack of value + note
   pairs, so a tenth of a rem per row compounds: loosening these five numbers
   back to their earlier values added 115px at 1920, which is roughly what a
   browser's own chrome takes off a 1080-tall screen -- the difference between
   the page fitting and the reader scrolling to reach ETF flows. Density is not
   a licence to drop a note: the qualifier stays, it just sits closer. */
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:.7rem .9rem; }
.card h2 { font-size:.85rem; margin:0 0 .5rem; letter-spacing:.05em;
           color:var(--accent); display:flex; justify-content:space-between;
           align-items:baseline; gap:.6rem; font-weight:600; }
.badge { font-size:.78rem; font-weight:400; color:var(--muted);
         white-space:nowrap; font-family:var(--mono); }
.badge.warn { color:var(--warn); }
.row { display:flex; justify-content:space-between; align-items:baseline;
       gap:1rem; padding:.16rem 0; }
.row + .row, .note + .row {
  border-top:1px solid color-mix(in srgb, var(--line) 45%, transparent); }
.label { color:var(--muted); font-size:.9rem; }
/* The categorical reading, sitting inside the label but not wearing its muted
   colour -- the point of promoting it out of the note is that a word is read
   before a number, and a word in the label's own grey is not. Only the rows
   whose measure defines a classifier have one, so the gaps down this column
   are the card saying which readings carry a direction, which it otherwise
   says only in prose. */
.cat { color:var(--text); font-weight:600; font-size:.92rem; margin-left:.45rem; }
.cat.up { color:var(--up); } .cat.down { color:var(--down); }
.cat.warn { color:var(--warn); }
.value { font-family:var(--mono); font-size:1.02rem; font-variant-numeric:tabular-nums;
         white-space:nowrap; font-weight:600; letter-spacing:-.01em; }
.value.up { color:var(--up); } .value.down { color:var(--down); }
.value.warn { color:var(--warn); }
.note { color:var(--muted); font-size:.8rem; line-height:1.32;
        padding:0 0 .18rem; margin-top:-.15rem; }
/* Sits between the heading and the first row, so it carries the gap the
   heading's margin would otherwise leave. */
.cardnote { color:var(--muted); font-size:.8rem; line-height:1.32;
            margin:-.3rem 0 .4rem; }
/* Muted so a coloured note stays secondary to the value it sits under. */
.note.up { color:color-mix(in srgb, var(--up) 78%, var(--muted)); }
.note.down { color:color-mix(in srgb, var(--down) 78%, var(--muted)); }
.note.warn { color:color-mix(in srgb, var(--warn) 78%, var(--muted)); }
.err { color:var(--warn); font-size:.88rem; }
.card.wide { grid-column:1/-1; }
/* The ask box is a second grid, below the data one rather than inside it,
   so an in-place update can replace every data card without touching it.
   The margin restores the gap the shared grid used to supply. */
.askgrid { margin-top:.85rem; }
/* The balance card is a band across the top, and the only thing above the card
   grid. Side by side with a strip of its own it was wrong in a way that only
   measures: a one-line strip left a 349px hole next to a 392px card, and the
   page went from 874px to 1223px at 1920 -- past the point where the six cards
   fit a 1080 screen, which is what the grid's column count was tuned around.

   Three columns, matching the grid below, so a reading lines up with the cards
   it summarises. Not auto-fit: at 1920 that packs six across, which wraps four
   of the six notes onto a second line and reads ragged. Six readings divide
   evenly only by 1, 2, 3 and 6, so a column count chosen by available width
   leaves a short last row at most widths -- which is the same reason the card
   grid below is tuned to three by measurement rather than by arithmetic.

   One column below 900px, where three columns would leave a reading about
   200px to put a label and a value in.

   ASCII only, like every comment in here: this stylesheet is copied verbatim
   into the PNG capture's SVG, where a stray glyph is a parse error rather than
   a typo. */
#composite { margin-bottom:.85rem; }
#composite:empty { display:none; }
.bgrid { display:grid; grid-template-columns:repeat(3,1fr); gap:0 1.6rem; }
@media (max-width:900px) { .bgrid { grid-template-columns:1fr; } }
/* One reading. The separators the stacked layout draws between rows are absent
   here deliberately: the column gaps already group them, and a rule under every
   cell of a two-row band reads as a table someone forgot to finish. */
.bcell { min-width:0; }
/* Title and bracket travel together as one flex child, so the badge stays at
   the right edge and a long list wraps under the title instead of being
   centred between the two. */
.ttl { min-width:0; }
/* The bracket itself is the label and wears the warning colour the strip used
   to; the readings inside it are content and are not shouted. Both drop the
   heading's weight, letter-spacing and accent -- a heading-styled sentence is
   a heading, and these are readings that happen to sit in one. */
.nota { color:var(--warn); font-weight:600; letter-spacing:0; margin-left:.5rem;
        font-size:.82rem; }
.notatext { color:var(--text); font-weight:400; }
.askform { display:flex; gap:.6rem; }
.askform input { flex:1; padding:.6rem .7rem; background:var(--bg);
                 color:var(--text); border:1px solid var(--line);
                 border-radius:6px; font:inherit; font-size:1rem; }
.askform button, .linkish { padding:.6rem 1.1rem; border:1px solid var(--line);
                 border-radius:6px; background:var(--accent); color:var(--bg);
                 font:inherit; font-size:.95rem; font-weight:600; cursor:pointer; }
.linkish { padding:.15rem .55rem; background:none; color:var(--muted);
           font-weight:400; font-size:.8rem; }
/* The card-level controls, kept together at the right of the heading. Spacing
   only: each control says what it is in its own text, so losing the stylesheet
   costs the arrangement and nothing else. */
.capbar { display:flex; align-items:baseline; gap:.35rem; }
.answer { white-space:pre-wrap; margin:.5rem 0; line-height:1.6;
          font-size:.98rem; }
.queries { margin:.5rem 0 0; font-size:.85rem; }
.queries summary { color:var(--muted); cursor:pointer; }
.queries pre { white-space:pre-wrap; word-break:break-word; margin:.4rem 0 .8rem;
               padding:.5rem .6rem; background:var(--bg); border:1px solid var(--line);
               border-radius:6px; font-family:var(--mono); font-size:.82rem;
               line-height:1.5; overflow-x:auto; }
.queries pre.out { color:var(--muted); }
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
        # The category rides inside the label rather than as a third flex
        # child: `.row` spreads its children apart, and a word placed between
        # the label and the value would drift to the middle of the card. Inside
        # it reads "Trend above" with the stylesheet gone, which is the order
        # it should be read in anyway.
        cat = (f'<span class="cat{tone}">{_esc(m.category)}</span>'
               if m.category else "")
        out.append(
            f'<div class="row"><span class="label">{_esc(m.label)}{cat}</span>'
            f'<span class="value{tone}">{_esc(m.value)}</span></div>'
        )
        if m.note:
            ntone = f" {m.note_tone}" if m.note_tone in ("up", "down", "warn") else ""
            out.append(f'<div class="note{ntone}">{_esc(m.note)}</div>')
    return "".join(out)


def _band_cells(metrics: list[Metric]) -> str:
    """The balance readings as grid cells rather than as one stacked list.

    A cell holds one reading — its row and its note — so the band can lay six
    of them out three across. Built by handing `_rows` a single metric rather
    than reimplementing its markup: the tone classes and the escaping are the
    same rules, and a second copy of them would drift from the first.
    """
    return "".join(f'<div class="bcell">{_rows([m])}</div>' for m in metrics)


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


ANSWER_RESULT_LINES = 12

# Shown in the ask box when no source offers live history, so the absence is
# stated rather than left to be inferred from a thinner answer.
NO_SCOPE_NOTE = (
    "No history available to query — answers use only the figures on this page."
)


def _analyst_scope(snapshot: dict) -> list[str]:
    """What the analyst can reach beyond this page, as each source describes it.

    Asked of the sources rather than read out of a known key here: which source
    owns history is not this renderer's business, and a page that reached into
    `sources["warehouse"]` by name would have to be edited every time that
    changed. Same shape as `notable()` — the source phrases its own line.
    """
    out: list[str] = []
    for name in snap.ordered_names(snapshot):
        block = snapshot["sources"][name]
        if not block.get("available"):
            continue
        mod = snap.module_for(name)
        if mod is None or not hasattr(mod, "analyst_scope"):
            continue
        try:
            line = mod.analyst_scope(block["data"])
        except Exception:
            # Never cost the ask box because a scope line failed to build.
            continue
        if line:
            out.append(line)
    return out


def _queries(calls) -> str:
    """What the analyst ran to answer, and what came back.

    Shown rather than summarised away. The answer's numbers are checkable only
    if the query behind them is visible, and that checkability is the whole
    reason the analyst reads a local warehouse instead of being asked what it
    remembers. Collapsed by default because it is evidence, not the answer.

    Everything here is escaped: the SQL was written by the model and the rows
    came out of a database filled from remote APIs. Neither is trusted markup.
    """
    calls = list(calls or [])
    if not calls:
        return ""
    blocks = []
    for call in calls:
        for value in (call.arguments or {}).values():
            blocks.append(f'<pre>{_esc(str(value).strip())}</pre>')
        lines = (call.result or "").splitlines()
        shown = "\n".join(lines[:ANSWER_RESULT_LINES])
        if len(lines) > ANSWER_RESULT_LINES:
            shown += f"\n… {len(lines) - ANSWER_RESULT_LINES} more lines"
        blocks.append(f'<pre class="out">{_esc(shown)}</pre>')
    label = f"{len(calls)} quer{'y' if len(calls) == 1 else 'ies'} run"
    return (f'<details class="queries"><summary>{_esc(label)}</summary>'
            f'{"".join(blocks)}</details>')


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
    # Beside the queries, and in the same register: both answer "what did this
    # answer actually have to work with". A page that shows queries when there
    # were some and says nothing when there were none reads as though the
    # thinner answer simply needed less.
    reason = answer.get("no_tools_reason")
    caveat = f'<div class="note warn">{_esc(reason)}</div>' if reason else ""
    return (
        '<section class="card wide"><h2>ANALYSIS</h2>'
        f'<div class="note">{q}</div>{body}'
        f'{_queries(answer.get("tool_calls"))}{caveat}{meta}</section>'
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


def _live_parts(snapshot: dict) -> dict[str, str]:
    """The regions that change with the data, wrapped and keyed by element id.

    Split out so the initial page and the fragment that updates an already-open
    tab come from one code path. An updater that rebuilt the markup its own way
    would drift from the page it is patching the first time either changed, and
    the drift would show as a region that silently stopped updating.
    """
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
        # A card-level qualifier sits under the heading, above the rows it
        # applies to. Its own class rather than `note`: `.note + .row` draws a
        # separator, which above the first row would read as a rule under the
        # heading rather than as a divider between readings.
        cardnote = (f'<div class="cardnote">{_esc(panel.note)}</div>'
                    if panel.note else "")
        cards.append(
            f'<section class="card"><h2>{_esc(panel.title)}{badge}</h2>'
            f'{cardnote}{_rows(panel.metrics)}{err}</section>'
        )

    # The notable readings, bracketed into the balance card's heading rather
    # than given a card of their own. They still lead the page — the heading is
    # the first thing on it — and they now cost no vertical space at all, which
    # a card 56px tall to hold one line of text could not say.
    #
    # Absent entirely when nothing qualifies, brackets included. A strip that
    # always finds something to say teaches the reader to stop looking at it,
    # and an empty `[NOTABLE: ]` is exactly that.
    #
    # Comma-separated as text, never a CSS separator: spacing supplied by the
    # stylesheet disappears with it, and the line would read
    # "[NOTABLE:7d volatility 10%30d volatility 22%]". A comma also binds to
    # the item it follows, so a wrapped line can never open with a stray
    # separator — which the old pipe needed a non-breaking space to guarantee.
    notable = _notable(snapshot)
    notable_html = ""
    if notable:
        notable_html = (
            '<span class="nota">[NOTABLE: '
            f'<span class="notatext">{", ".join(_esc(n) for n in notable)}</span>]'
            '</span>'
        )

    # The balance card. A card like any other, so it inherits the row layout
    # and every qualifier renders — but built here rather than in the grid
    # loop, because it belongs to no source and its badge counts readings
    # rather than reporting a cache age.
    balance = composite.rows(snapshot)
    balance_html = ""
    # Built when there are readings *or* something to lead with. An ingested
    # snapshot whose sources this build has no renderer for yields no rows, and
    # the notable list would then have nowhere to go — including the one entry
    # it can always produce, which is that a source is unavailable.
    if balance or notable_html:
        badge = ""
        if balance:
            cls = "badge" if composite.complete(balance) else "badge warn"
            badge = (f'<span class="{cls}">'
                     f'{_esc(composite.coverage_label(balance))}</span>')
        # Two flex children, not three: `h2` spreads its children apart, so a
        # bracket added as a sibling would float to the middle of the card.
        # Grouped with the title it stays beside it and wraps under it.
        body = (f'<div class="cardnote">{_esc(composite.NOTE)}</div>'
                f'<div class="bgrid">{_band_cells(balance)}</div>') if balance else ""
        balance_html = (
            f'<section class="card"><h2><span class="ttl">'
            f'{_esc(composite.TITLE)}{notable_html}</span>{badge}</h2>'
            f'{body}</section>'
        )

    # Each part is wrapped in the element the updater patches. The wrapper is
    # always present even when its contents are empty — the notable strip comes
    # and goes — so a region can vanish and return without the ones around it
    # moving. It is also what `#notable:empty` tests, which is how the balance
    # card takes the whole row on a day with nothing to lead with.
    return {
        "ticks": f'<div class="ticks" id="ticks">{ticks}</div>',
        "stamp": f'<div class="meta" id="stamp">{_esc(generated)} UTC</div>',
        "composite": f'<div id="composite">{balance_html}</div>',
        "cards": f'<div class="grid" id="cards">{"".join(cards)}</div>',
    }


def render_live(snapshot: dict) -> str:
    """Just those regions, as a fragment, for a page updating itself in place.

    Not a document: the updater reads the parts it knows by id and ignores the
    rest, so a new region can be added here and picked up by adding its id to
    `LIVE_IDS` — the script itself never changes.

    Note what is *not* here: the ask box. That is the point. A snapshot tick
    must not be able to replace the field someone is typing into.
    """
    return "".join(_live_parts(snapshot).values())


def _updater_script(url: str, seconds: int) -> str:
    """The in-place updater, pointed at `url` and ticking every `seconds`."""
    return (
        _UPDATER_JS
        # Both values are this program's own constants rather than anything
        # from a snapshot, but they are escaped like every other string that
        # reaches the page: `<` cannot be allowed to start a tag from inside a
        # script, and json.dumps does not escape it.
        .replace("URL_JSON", json.dumps(url).replace("<", "\\u003c"))
        .replace("IDS_JSON", json.dumps(list(LIVE_IDS)))
        .replace("EVERY_MS", str(int(seconds) * 1000))
    )


def _capture_script() -> str:
    """The PNG capture, pointed at the regions it is allowed to draw."""
    return (
        _CAPTURE_JS
        .replace("IDS_JSON", json.dumps(list(CAPTURE_IDS)))
        .replace("SCALE_N", str(CAPTURE_SCALE))
    )


def render_html(snapshot: dict, *, title: str = "BTC DASHBOARD",
                refresh: int | None = REFRESH_SECONDS,
                ask: bool = False, answer: dict | None = None,
                live_endpoint: str | None = None,
                capture: bool = False) -> str:
    """The page. `ask` adds the analyst box, which needs a server behind it.

    `live_endpoint` is the URL of something serving `render_live()`. Given one,
    the page updates its data regions from it in place rather than reloading
    the document — which is what lets someone type a question while the numbers
    keep moving. Without one (a file, a static server) the whole document
    reloads on a meta refresh, as before.

    `capture` adds the PNG buttons. They sit in the ask box's heading beside
    the refresh control, so it needs `ask` too — and that placement is the
    reason the buttons keep themselves out of their own image, the ask box
    being the one region `CAPTURE_IDS` leaves out.
    """
    parts = _live_parts(snapshot)

    ask_html = ""
    if ask:
        scope = _analyst_scope(snapshot)
        scope_html = "".join(
            f'<div class="note">{_esc(line)}</div>' for line in scope
        ) or f'<div class="note">{_esc(NO_SCOPE_NOTE)}</div>'

        # Its own grid, deliberately outside the live region: an update
        # replaces the data cards wholesale, and this must survive it. It
        # changes when an answer arrives, never on a tick.
        # A button, not a link: nothing is fetched, the image is drawn here.
        # `type="button"` because these sit beside a form and a default submit
        # would post the question instead of drawing anything.
        capture_html = (
            '<button class="linkish" type="button" data-capture="copy">'
            'copy PNG</button>'
            '<button class="linkish" type="button" data-capture="save">'
            'save PNG</button>'
        ) if capture else ""
        # Outside the heading's control bar: the result of a capture is a
        # sentence, and a sentence in a flex row of buttons reflows them every
        # time it changes.
        capture_note = '<div class="note" id="capnote"></div>' if capture else ""

        ask_html = (
            '<section class="grid askgrid">'
            '<section class="card wide"><h2>ASK'
            '<span class="capbar">'
            + capture_html +
            '<form method="post" action="/refresh" style="margin:0">'
            '<button class="linkish" type="submit">refresh data</button>'
            '</form></span></h2>'
            + capture_note +
            '<form method="post" action="/ask" class="askform">'
            '<input name="q" autofocus autocomplete="off" '
            'placeholder="ask a question about this snapshot">'
            '<button type="submit">Ask</button></form>'
            # Above the cost note on purpose: this is what you need while
            # composing the question, not after sending it.
            + scope_html
            + '<div class="note">Sent to the configured provider using the key on '
            'this machine — each question costs money. The analyst sees the '
            'facts on this page, including which are cached or stale.</div>'
            '</section>'
            + _answer_card(answer)
            + '</section>'
        )

    meta_refresh = (
        f'<meta http-equiv="refresh" content="{int(refresh)}">' if refresh else ""
    )
    updater = ""
    if refresh and live_endpoint:
        # The meta refresh survives as the no-script fallback. It still clears
        # the ask box, which is the whole problem — but a page that stops
        # updating entirely, with no sign that it has, is worse.
        meta_refresh = f"<noscript>{meta_refresh}</noscript>"
        updater = _updater_script(live_endpoint, refresh)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
{meta_refresh}<title>{_esc(title)}</title>
<link rel="icon" type="image/png" sizes="32x32" href="{_favicon_png_data_uri()}">
<link rel="icon" type="image/svg+xml" href="{_favicon_data_uri()}">
<style>{CSS}</style></head>
<body>
<header id="pagehead">
  <h1>{_esc(title)}</h1>
  {parts['ticks']}
  {parts['stamp']}
</header>
<main>
{parts['composite']}
{parts['cards']}{ask_html}
</main>
<footer id="pagefoot">Data: local node + DuckDB · price: CoinGecko · ETF: Farside.
Percentile windows and volatility annualisation are stated on each figure —
compare those, not bare levels, against any external source.</footer>
{updater}{_capture_script() if capture else ""}
</body></html>
"""
