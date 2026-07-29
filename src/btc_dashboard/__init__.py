"""BTC-focused analytics: on-chain history, live node, price/SMA, and ETF flows.

Collect a snapshot and render it:

    from btc_dashboard import Config, build_snapshot, render_snapshot
    snap = build_snapshot(Config.from_env())
    print(render_snapshot(snap))

Or reason over it:

    from btc_dashboard import ask
    print(ask(snap, "what changed in flows this week?", Config.from_env()).text)

The snapshot is a plain JSON-serializable dict — see `snapshot.py` for its
shape. It is the contract every consumer shares, including the planned HTTP
service.
"""
from .analyst import ask, build_context
from .config import Config
from .render import render as render_snapshot
from .snapshot import SCHEMA_VERSION, build as build_snapshot

# Exported names are deliberately *_snapshot rather than the bare module-level
# names: `render` and `snapshot` are also submodules, and re-exporting the
# functions under those names would shadow the modules on the package object.
__all__ = [
    "Config",
    "SCHEMA_VERSION",
    "ask",
    "build_context",
    "build_snapshot",
    "render_snapshot",
]
