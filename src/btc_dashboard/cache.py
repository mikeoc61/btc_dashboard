"""Per-source TTL cache for the data plane.

Two jobs, one file:

1. **Fresh-within-TTL** — a source whose upstream changes at most daily is
   served from disk rather than refetched. This is what keeps N clients of the
   eventual JSON service from each triggering a Farside scrape.
2. **Stale-on-failure** — when the TTL has expired *and* the live collection
   fails, the expired copy is served flagged `stale` rather than the block
   disappearing. Yesterday's finalized flows beat nothing, provided the reader
   is told they're old. That rescue is bounded by `STALE_MAX_AGE`: past it the
   block goes unavailable, because on a host where the live path always fails
   an unbounded fallback serves the same copy forever.

Only sources that declare a `CACHE_TTL` participate. Live tip state (spot
price, mempool, block height) deliberately does not: serving a 40-minute-old
mempool as current would be worse than not showing it.

`cached` and `stale` mean different things and both appear in the snapshot:
`cached` is "served from disk, within policy"; `stale` is "served from disk
because the live path failed". A block can be cached without being stale.

Writes are atomic (temp file + `os.replace`), so concurrent readers never see
a half-written payload and two processes racing produce one winner rather than
a corrupt file.
"""
from __future__ import annotations

import datetime
import json
import os
import tempfile
from pathlib import Path

from .sources import SourceResult, unavailable

CACHED_AT = "cached_at"

# How long an expired copy may go on rescuing a failed live path.
#
# Without a bound the fallback never ends. On a host where the live path always
# fails — a laptop with no warehouse file — the entry is re-aged and re-served
# on every run, so the block keeps asserting figures that no future run can
# ever correct. That is not hypothetical: a payload written from a database
# this project had never read stayed on the panel for three days, badged
# accurately as old and wrong in every value.
#
# Note what the badge cannot do. `_rederive` keeps the *age* honest, which is
# the reason a stale block is safe to show at all — but age is not provenance,
# and no amount of labelling turns an indefinitely-served copy into a current
# reading.
#
# Four days is the shortest bound that still covers the longest transient the
# cached sources actually see: a Friday-evening failure across a weekend and a
# Monday holiday runs about three and a half days. It also has to clear three,
# because `test_rederive_also_runs_on_the_stale_fallback_path` pins a
# three-day-old payload as still worth serving — this codebase's own statement
# about where the line sits.
STALE_MAX_AGE = 4 * 24 * 3600


def path_for(cfg, name: str) -> Path:
    return Path(cfg.cache_dir) / f"{name}.json"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc)


def read(path: Path) -> tuple[SourceResult, float] | None:
    """Return `(result, age_seconds)` from the cache, or None if unusable.

    Every failure path returns None: an absent file, unreadable JSON, a missing
    or unparseable timestamp. A cache that cannot be trusted is treated as a
    cache miss, never as an error.
    """
    try:
        payload = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        stamp = datetime.datetime.fromisoformat(payload.get(CACHED_AT))
    except (TypeError, ValueError):
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=datetime.timezone.utc)

    age = (_now() - stamp).total_seconds()
    if age < 0:
        # Timestamp in the future — a clock correction, or a file written by a
        # host running fast. Treat as expired rather than trusting it until the
        # clocks agree again, which could be indefinitely.
        age = float("inf")

    block = payload.get("source")
    if not isinstance(block, dict):
        return None

    result = SourceResult(
        name=payload.get("name") or "",
        available=bool(block.get("available")),
        data=block.get("data") or {},
        error=block.get("error"),
        as_of=block.get("as_of"),
    )
    return result, age


def write(path: Path, result: SourceResult) -> bool:
    """Persist a result. Returns False on failure — never raises.

    A cache we cannot write is not a reason to lose a good collection.
    """
    payload = {
        CACHED_AT: _now().isoformat(),
        "name": result.name,
        "source": result.to_json(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(payload, fh, indent=2, default=str)
            os.replace(tmp, path)
        except BaseException:
            # Don't leave the temp file behind if the write or rename failed.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except (OSError, TypeError, ValueError):
        return False
    return True


def collect(mod, cfg, *, refresh: bool = False) -> SourceResult:
    """Collect a source, going through its cache when it declares a TTL.

    Order matters: a fresh cache short-circuits before any network or database
    work, and the expired copy is kept in hand so it can still rescue a failed
    live collection.
    """
    ttl = getattr(mod, "CACHE_TTL", None)
    if not ttl:
        return mod.collect(cfg)

    ttl = _resolve_ttl(cfg, ttl)
    path = path_for(cfg, mod.NAME)
    entry = None if refresh else read(path)

    if entry is not None and ttl > 0:
        result, age = entry
        if age <= ttl and result.available:
            return _rederive(mod, result).as_cached(age, ttl)

    fresh = mod.collect(cfg)
    if fresh.available:
        write(path, fresh)
        return fresh

    # Live collection failed. An expired copy is better than an empty block,
    # so long as it is labelled — including the reason the live path failed —
    # and so long as it has not aged past the point where it describes a
    # different market than the one being asked about. See STALE_MAX_AGE.
    if entry is not None:
        result, age = entry
        if result.available:
            if age > STALE_MAX_AGE:
                return unavailable(mod.NAME, _discarded(fresh.error, age))
            return _rederive(mod, result).as_stale(age, fresh.error)
    return fresh


def _discarded(error: str | None, age: float) -> str:
    """Why the block is empty when a cached copy is sitting on disk.

    Reporting only the live failure would send a reader hunting for a missing
    database when the more useful fact is that a copy was there and was refused
    for its age — a different problem with a different fix.
    """
    if age == float("inf"):
        detail = "a cached copy stamped in the future was discarded"
    else:
        detail = (f"a cached copy {age / 86400:.1f} days old was discarded, "
                  f"past the {STALE_MAX_AGE // 86400}-day limit")
    return f"{error}; {detail}" if error else detail


def _rederive(mod, result: SourceResult) -> SourceResult:
    """Recompute a source's time-relative fields against the clock *now*.

    Some fields are relative to when they were computed, not to the data:
    "1d ago", "2 days behind". Serving them straight from cache makes a
    two-day-old payload claim it is a day old — and on the stale-fallback path
    that is exactly when the reader most needs the truth. A source that has
    such fields exposes `refresh_derived(data)`; everything else is untouched.
    """
    hook = getattr(mod, "refresh_derived", None)
    if hook is None or not result.available:
        return result
    try:
        return SourceResult(**{**result.__dict__, "data": hook(dict(result.data))})
    except Exception:
        # A broken hook must not cost the cached data.
        return result


def _resolve_ttl(cfg, default: int) -> int:
    ttl = getattr(cfg, "cache_ttl", None)
    return default if ttl is None else ttl
