"""Render a snapshot as terminal text.

Presentation only — this module never touches a source or a network. Given the
same snapshot it always produces the same text, which is what makes the CLI
output reproducible from a saved `--json` payload.
"""
from __future__ import annotations

import os
import sys

from . import snapshot as snap

RULE = "─" * 60

# The basic 8-colour codes only. A terminal maps these through the user's own
# theme, so they stay legible on light and dark backgrounds alike; 256-colour
# or truecolour values would pick a specific hue and lose that. Nothing here
# sets a background, and no colour carries meaning on its own — every marker
# still reads correctly in plain text.
BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
CYAN, YELLOW, RED = "\033[36m", "\033[33m", "\033[31m"


def supports_color(stream=None) -> bool:
    """Whether to emit ANSI codes.

    Honours the NO_COLOR convention (any value, including empty, disables) and
    FORCE_COLOR for pipelines that want them anyway. Otherwise colour only when
    writing to a real terminal, so `--json`, a redirect to a file, or a pipe
    into another tool stay clean.
    """
    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    stream = stream if stream is not None else sys.stdout
    if os.environ.get("TERM") == "dumb":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


class _Paint:
    """Wraps text in a code, or returns it untouched when colour is off."""

    def __init__(self, enabled: bool):
        self.enabled = enabled

    def __call__(self, text: str, *codes: str) -> str:
        # Empty text is passed through unwrapped: painting "" would emit a
        # bare set-and-reset pair that does nothing but clutter the output.
        if not self.enabled or not codes or not text:
            return text
        return "".join(codes) + text + RESET


def human_age(seconds) -> str:
    """Compact age for a cache marker: 45s, 12m, 3h, 2d."""
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return "?"
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"


def _cache_flag(block: dict) -> str:
    """Marker distinguishing a within-TTL cache hit from an expired fallback.

    Both are served from disk, but they mean different things to the reader:
    `cached` is normal operation, `STALE` means the live path failed and this
    is older than policy allows.
    """
    if block.get("stale"):
        age = block.get("cache_age_seconds")
        return f" [STALE {human_age(age)}]" if age is not None else " [STALE]"
    if block.get("cached"):
        return f" [cached {human_age(block.get('cache_age_seconds'))}]"
    return ""


def render(snapshot: dict, *, show_errors: bool = True, color: bool | None = None) -> str:
    """Render the panel. `color=None` auto-detects; True/False force it."""
    paint = _Paint(supports_color() if color is None else color)
    lines: list[str] = []
    generated = snapshot["generated_at"][:19].replace("T", " ")
    lines.append(paint(f"BTC DASHBOARD — {generated} UTC", BOLD))
    lines.append(paint(RULE, DIM))

    for name in snap.ordered_names(snapshot):
        block = snapshot["sources"][name]
        title = snap.TITLES.get(name, name.upper())

        if not block["available"]:
            if show_errors:
                lines.append(
                    paint(title, DIM, BOLD) + paint(f": unavailable — {block.get('error')}", DIM)
                )
                lines.append("")
            continue

        flag = _cache_flag(block)
        lines.append(
            paint(title, BOLD, CYAN)
            + paint(flag, YELLOW if block.get("stale") else DIM)
        )
        mod = snap.module_for(name)
        if mod is None:
            # An ingested snapshot from a newer service can carry a source this
            # build has no renderer for. Say so plainly rather than dropping it.
            body = [f"(no renderer in this build — see --json for the raw data)"]
        else:
            try:
                body = mod.render_lines(block["data"])
            except Exception as e:
                body = [f"render failed: {type(e).__name__}: {e}"]
        lines.extend(f"  {line}" for line in body)
        if block.get("stale") and block.get("error") and show_errors:
            lines.append(paint(f"  live refresh failed: {block['error']}", YELLOW))
        lines.append("")

    missing = snap.missing(snapshot)
    if missing and not show_errors:
        lines.append(paint(f"unavailable: {', '.join(missing)}", DIM))
    return "\n".join(lines).rstrip() + "\n"
