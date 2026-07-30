"""Command-line entry point."""
from __future__ import annotations

import argparse
import json
import sys

from . import analyst, render, snapshot
from .config import Config

EPILOG = """\
examples:
  btc-dashboard                          full panel
  btc-dashboard --json                   the snapshot, for piping
  btc-dashboard --only flows,price       just those blocks
  btc-dashboard --ask "is the flow picture consistent with the price move?"
  btc-dashboard --context                what the analyst would be told, without asking
  btc-dashboard --refresh                bypass the cache and re-collect

on-chain and ETF flow data are cached for 60 minutes (both change at most
daily); price and node are live tip state and never cached.

  # ingest a snapshot instead of collecting one, then analyse it locally
  btc-dashboard --from https://pi.local/btc/snapshot.json --ask "what changed?"
  btc-dashboard --json > snap.json && btc-dashboard --from snap.json

the LLM never runs server-side: --ask reads ANTHROPIC_API_KEY on THIS machine
and sends the snapshot from here. A snapshot service serves raw JSON only.

exit codes:
  0  ok        1  no source available        2  bad usage / analyst failed
"""


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="btc-dashboard",
        description="BTC analytics: live network, on-chain history, price/SMA, and ETF flows.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--json", action="store_true", help="emit the snapshot as JSON")
    p.add_argument(
        "--from",
        dest="origin",
        metavar="URL|PATH|-",
        help="ingest a snapshot instead of collecting one (http(s) URL, file, or - for stdin)",
    )
    p.add_argument(
        "--only",
        help=f"comma-separated subset of: {', '.join(snapshot.SOURCE_NAMES)}",
    )
    p.add_argument("--ask", metavar="QUESTION", help="send the snapshot to the LLM analyst")
    p.add_argument(
        "--context",
        action="store_true",
        help="print the analyst's context block and exit (no API call)",
    )
    p.add_argument("--db", help="warehouse path (default: ~/data/market.duckdb)")
    p.add_argument("--model", help="model id for --ask")
    p.add_argument(
        "--effort",
        choices=("low", "medium", "high", "xhigh", "max"),
        help="reasoning effort for --ask (default: high)",
    )
    p.add_argument("--timeout", type=int, help="per-source network timeout, seconds")
    p.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the cache and re-collect (on-chain and ETF flows are cached 60m)",
    )
    p.add_argument(
        "--cache-ttl",
        type=int,
        metavar="SECONDS",
        help="cache lifetime for cached sources (default 3600; 0 disables)",
    )
    p.add_argument("--quiet", action="store_true", help="hide unavailable-source notes")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    only = None
    if args.only:
        if args.origin:
            # --only selects what to collect; an ingested snapshot is already
            # whatever the service chose to include. Silently filtering it here
            # would look like the service returned less than it did.
            print("--only applies to collection and cannot be used with --from",
                  file=sys.stderr)
            return 2
        only = tuple(s.strip() for s in args.only.split(",") if s.strip())
        unknown = [s for s in only if s not in snapshot.SOURCE_NAMES]
        if unknown:
            print(
                f"unknown source(s): {', '.join(unknown)}\n"
                f"available: {', '.join(snapshot.SOURCE_NAMES)}",
                file=sys.stderr,
            )
            return 2

    cfg = Config.from_env(
        db_path=args.db, model=args.model, effort=args.effort, timeout=args.timeout,
        # 0 is a meaningful value here (cache disabled), so it is passed through
        # explicitly rather than filtered out as falsy by Config.replace.
        cache_ttl=args.cache_ttl if args.cache_ttl is not None else None,
    )

    if args.origin:
        try:
            snap = snapshot.load(args.origin, timeout=cfg.timeout)
        except snapshot.SnapshotError as e:
            print(f"could not read snapshot: {e}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"could not fetch {args.origin}: {e}", file=sys.stderr)
            return 2
    else:
        snap = snapshot.build(cfg, only=only, refresh=args.refresh)

    if args.json:
        print(json.dumps(snap, indent=2, default=str))
        return 0 if snapshot.available(snap) else 1

    if args.context:
        print(analyst.build_context(snap))
        return 0

    print(render.render(snap, show_errors=not args.quiet), end="")

    if not snapshot.available(snap):
        print("no source returned data", file=sys.stderr)
        return 1

    if args.ask:
        result = analyst.ask(snap, args.ask, cfg)
        if not result.ok:
            print(f"\nanalyst unavailable: {result.error}", file=sys.stderr)
            return 2
        print(f"\nANALYSIS\n{'─' * 60}\n{result.text}")
        print(
            f"\n[{result.model} · {result.input_tokens} in / "
            f"{result.output_tokens} out]",
            file=sys.stderr,
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
