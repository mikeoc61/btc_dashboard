"""Assemble every source into one snapshot.

The snapshot dict is **the contract**. The CLI renders it, the analyst reasons
over it, and the planned HTTP service will serve it verbatim. Nothing
downstream re-reads a source, so there is exactly one place where "what the
tool knows right now" is defined.

Shape:

    {
      "schema_version": 1,
      "generated_at": "<ISO-8601 UTC>",
      "asset": "btc",
      "sources": {
        "<name>": {"available", "stale", "as_of", "error", "data"},
        ...
      }
    }

Every source key is always present, available or not — a consumer can index
`sources["node"]` without guarding, and an absent node is a stated fact rather
than a missing key. `schema_version` exists so the service can add fields
without breaking a pinned client.

**The service boundary runs through this module.** `build()` is the data plane:
it collects sources and produces the payload, and it never reaches an LLM or
reads a credential. `load()` is the client half, ingesting that payload from a
URL, a file, or stdin. Everything LLM-shaped lives in `analyst.py` and runs
only on the operator's machine under their own credentials — so a deployed
service holds no API key and cannot be made to spend one.
"""
from __future__ import annotations

import datetime
import re
from concurrent.futures import ThreadPoolExecutor

from . import cache
from .sources import SourceResult, flows, node, price, warehouse

SCHEMA_VERSION = 1

# A leading URL scheme. Matched against the whole origin so `ftp://host/x` is
# recognised as a scheme rather than mistaken for a relative path.
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*://")

# Order is the render order. Live state first, then history, then flows —
# roughly newest-to-oldest, which is how the numbers get read.
SOURCES = (price, node, warehouse, flows)
SOURCE_NAMES = tuple(s.NAME for s in SOURCES)

TITLES = {
    "price": "PRICE",
    "node": "NETWORK (live)",
    "warehouse": "ON-CHAIN (daily)",
    "flows": "ETF FLOWS (US SPOT)",
}


def _collect_one(mod, cfg, refresh: bool) -> SourceResult:
    # collect() is documented never to raise, but a source is one bad edit away
    # from breaking that promise, and one source must never take down the
    # snapshot.
    try:
        return cache.collect(mod, cfg, refresh=refresh)
    except Exception as e:
        return SourceResult(
            name=mod.NAME, available=False, error=f"collector raised {type(e).__name__}: {e}"
        )


def build(cfg, only: tuple[str, ...] | None = None, *, refresh: bool = False) -> dict:
    """Collect all sources and return the snapshot.

    Sources are independent and three of the four are I/O-bound on different
    hosts, so they run concurrently — total time is the slowest source, not the
    sum.

    Sources declaring a `CACHE_TTL` are served from disk when a fresh copy
    exists; `refresh=True` bypasses that and rewrites the cache.
    """
    mods = [m for m in SOURCES if only is None or m.NAME in only]
    with ThreadPoolExecutor(max_workers=len(mods) or 1) as pool:
        results = list(pool.map(lambda m: _collect_one(m, cfg, refresh), mods))

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "asset": "btc",
        "sources": {r.name: r.to_json() for r in results},
    }


class SnapshotError(ValueError):
    """An ingested payload isn't a snapshot this version can read."""


def validate(payload) -> dict:
    """Check an ingested payload before anything downstream trusts its shape.

    Locally-built snapshots are correct by construction; ingested ones are not.
    A payload arriving over the wire may come from a different version of this
    tool, or from something that isn't this tool at all, so the structural
    guarantees the renderer and analyst rely on get checked once, here.
    """
    if not isinstance(payload, dict):
        raise SnapshotError(f"expected a JSON object, got {type(payload).__name__}")

    version = payload.get("schema_version")
    if version is None:
        raise SnapshotError("payload has no schema_version — not a snapshot")
    if not isinstance(version, int):
        raise SnapshotError(f"schema_version must be an integer, got {version!r}")
    if version > SCHEMA_VERSION:
        raise SnapshotError(
            f"snapshot is schema v{version}, this build understands up to "
            f"v{SCHEMA_VERSION} — upgrade the client"
        )

    # Checked because `render` indexes it directly — a payload without it
    # validated cleanly and then took out the whole panel with a KeyError.
    # Presence and type only, never a parsed timestamp: consumers slice this
    # for display, and demanding ISO-8601 would refuse a producer whose format
    # is merely different rather than wrong.
    generated = payload.get("generated_at")
    if not isinstance(generated, str):
        raise SnapshotError(
            "payload has no string 'generated_at' — every consumer stamps the "
            f"panel with it (got {type(generated).__name__})"
        )

    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise SnapshotError("payload has no 'sources' object")
    for name, block in sources.items():
        if not isinstance(block, dict) or "available" not in block:
            raise SnapshotError(f"source {name!r} is malformed")
        # `available` decides which of two entirely different paths a consumer
        # takes, and every one of them tests it for truth. A string "false" is
        # truthy, so a dead source reported that way is read as healthy: its
        # `error` — the stated reason, the thing the analyst is supposed to
        # reason about — is never reached, and `missing()` reports nothing
        # missing. Being *told* what is unavailable is a contract of this
        # tool, so the flag carrying it has to be a flag.
        if not isinstance(block["available"], bool):
            raise SnapshotError(
                f"source {name!r} has a non-boolean 'available' "
                f"({block['available']!r}) — a dead source would read as healthy"
            )
        # Renderers are handed this and call `.get` on it. A non-mapping is
        # caught downstream and reported as "render failed", which blames this
        # build for the peer's payload; refused here it names the real cause.
        if block["available"] and not isinstance(block.get("data"), dict):
            raise SnapshotError(
                f"source {name!r} is available but its 'data' is not an object "
                f"(got {type(block.get('data')).__name__})"
            )

    return payload


def load(origin: str, timeout: int = 20) -> dict:
    """Ingest a snapshot from a URL, a file path, or `-` for stdin.

    This is the client half of the eventual service split: the data plane
    serves JSON, and every consumer here reads it through this one function.
    """
    import json
    import sys

    if origin == "-":
        raw = sys.stdin.read()
    elif origin.startswith(("http://", "https://")):
        import urllib.request

        req = urllib.request.Request(origin, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8")
    elif _SCHEME_RE.match(origin):
        # Anything else carrying a scheme is refused rather than guessed at — a
        # local path is just a path, and file:// would only blur the boundary
        # this seam exists to draw.
        raise SnapshotError(f"unsupported scheme in {origin!r}; use http(s), a path, or -")
    else:
        from pathlib import Path

        raw = Path(origin).read_text()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SnapshotError(f"{origin}: not valid JSON: {e}") from e
    return validate(payload)


def module_for(name: str):
    """The module owning a source, or None if this build doesn't know it.

    An ingested snapshot from a newer service may carry sources this client has
    no renderer for. That's a forward-compatible situation, not an error — the
    caller reports the block generically instead of crashing.
    """
    return next((m for m in SOURCES if m.NAME == name), None)


def ordered_names(snapshot: dict) -> list[str]:
    """Source keys in render order: known ones first, then anything new."""
    present = list(snapshot["sources"])
    known = [n for n in SOURCE_NAMES if n in present]
    return known + [n for n in present if n not in SOURCE_NAMES]


def available(snapshot: dict) -> list[str]:
    return [k for k, v in snapshot["sources"].items() if v["available"]]


def missing(snapshot: dict) -> list[str]:
    return [k for k, v in snapshot["sources"].items() if not v["available"]]
