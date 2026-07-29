"""Render a snapshot as terminal text.

Presentation only — this module never touches a source or a network. Given the
same snapshot it always produces the same text, which is what makes the CLI
output reproducible from a saved `--json` payload.
"""
from __future__ import annotations

from . import snapshot as snap

RULE = "─" * 60


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

        flag = " [STALE]" if block.get("stale") else ""
        lines.append(f"{title}{flag}")
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
        if block.get("stale") and block.get("error"):
            lines.append(f"  served from cache — live fetch failed: {block['error']}")
        lines.append("")

    missing = snap.missing(snapshot)
    if missing and not show_errors:
        lines.append(f"unavailable: {', '.join(missing)}")
    return "\n".join(lines).rstrip() + "\n"
