"""Render a snapshot as terminal text.

Presentation only — this module never touches a source or a network. Given the
same snapshot it always produces the same text, which is what makes the CLI
output reproducible from a saved `--json` payload.
"""
from __future__ import annotations

from . import snapshot as snap

RULE = "─" * 60


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


def render(snapshot: dict, *, show_errors: bool = True) -> str:
    lines: list[str] = []
    generated = snapshot["generated_at"][:19].replace("T", " ")
    lines.append(f"BTC DASHBOARD — {generated} UTC")
    lines.append(RULE)

    for name in snap.ordered_names(snapshot):
        block = snapshot["sources"][name]
        title = snap.TITLES.get(name, name.upper())

        if not block["available"]:
            if show_errors:
                lines.append(f"{title}: unavailable — {block.get('error')}")
                lines.append("")
            continue

        lines.append(f"{title}{_cache_flag(block)}")
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
            lines.append(f"  live refresh failed: {block['error']}")
        lines.append("")

    missing = snap.missing(snapshot)
    if missing and not show_errors:
        lines.append(f"unavailable: {', '.join(missing)}")
    return "\n".join(lines).rstrip() + "\n"
