"""Command-line entry point."""
from __future__ import annotations

import argparse
import json
import sys

from . import analyst, render, snapshot
from .config import Config
from .text import safe_block, safe_text

EPILOG = """\
examples:
  btc-dashboard                          full panel
  btc-dashboard --json                   the snapshot, for piping
  btc-dashboard --only flows,price       just those blocks
  btc-dashboard --ask "is the flow picture consistent with the price move?"
  btc-dashboard --context                what the analyst would be told, without asking
  btc-dashboard --refresh                bypass the cache and re-collect
  btc-dashboard --ask "..." --model deepseek/deepseek-chat
  btc-dashboard --ask "..." --provider ollama --model llama3

on-chain and ETF flow data are cached for 60 minutes (both change at most
daily); price and node are live tip state and never cached.

  # ingest a snapshot instead of collecting one, then analyse it locally
  btc-dashboard --from https://pi.local/btc/snapshot.json --ask "what changed?"
  btc-dashboard --json > snap.json && btc-dashboard --from snap.json

the LLM never runs server-side: --ask reads the provider's key on THIS machine
and sends the snapshot from here. A snapshot service serves raw JSON only.
providers: anthropic (default), openai, deepseek, openrouter, ollama (local).

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
    p.add_argument("--html", action="store_true",
                   help="emit a self-contained HTML page instead of the panel")
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
    p.add_argument(
        "--provider",
        help="LLM provider for --ask (a 'provider/model' prefix on --model wins)",
    )
    p.add_argument("--model", help="model id for --ask, optionally 'provider/model'")
    p.add_argument(
        "--no-tools", action="store_true",
        help="answer from the snapshot alone; do not let --ask query the warehouse",
    )
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
    p.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colourise the panel (default: auto — only when writing to a terminal)",
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
        db_path=args.db, provider=args.provider, model=args.model,
        effort=args.effort, timeout=args.timeout,
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

    if args.html:
        from . import html as html_render

        print(html_render.render_html(snap))
        return 0 if snapshot.available(snap) else 1

    if args.context:
        print(analyst.build_context(snap))
        return 0

    color = {"auto": None, "always": True, "never": False}[args.color]
    print(render.render(snap, show_errors=not args.quiet, color=color), end="")

    if not snapshot.available(snap):
        print("no source returned data", file=sys.stderr)
        return 1

    if args.ask:
        result = analyst.ask(snap, args.ask, cfg, use_tools=not args.no_tools)
        if not result.ok:
            # Provider errors can carry an API response body, so they are
            # bounded like any other text this process did not write.
            print(f"\nanalyst unavailable: {safe_text(result.error)}",
                  file=sys.stderr)
            return 2
        paint = render._Paint(render.supports_color() if color is None else color)
        # The answer keeps its own line structure — it is prose, and
        # collapsing it would destroy it — but not the characters that let a
        # remote model repaint or reorder this terminal. A hostile snapshot
        # steers the model; the model's output lands here.
        print(
            f"\n{paint('ANALYSIS', render.BOLD, render.CYAN)}\n"
            f"{paint('─' * 60, render.DIM)}\n{safe_block(result.text)}"
        )
        # The queries are shown, not just counted. A figure the answer leans on
        # that came from a query nobody saw cannot be checked, and checking the
        # numbers is the whole reason this reads a local warehouse rather than
        # asking the model what it remembers.
        for call in result.tool_calls:
            print(
                f"\n{paint('QUERIED', render.DIM)} "
                f"{paint(safe_text(call.name), render.DIM)}\n"
                + "\n".join(
                    f"  {line}" for line in _tool_call_lines(call)
                ),
                file=sys.stderr,
            )
        # Said to the reader, not only to the model. Without it a snapshot-only
        # answer is indistinguishable from one that checked the history — and
        # over `--from`, snapshot-only is the normal case.
        if result.no_tools_reason:
            print(f"\n{paint(result.no_tools_reason, render.DIM)}", file=sys.stderr)
        print(
            f"\n[{result.provider}/{safe_text(result.model)} · "
            f"{result.input_tokens} in / "
            f"{result.output_tokens} out"
            + (f" · {len(result.tool_calls)} quer"
               f"{'y' if len(result.tool_calls) == 1 else 'ies'}"
               if result.tool_calls else "")
            + "]",
            file=sys.stderr,
        )

    return 0


def _tool_call_lines(call, max_result_lines: int = 6) -> list[str]:
    """A tool call rendered for the terminal: what was asked, and a peek back.

    The arguments in full — they are the thing being audited — and only the
    head of the result, which can run to hundreds of rows the model needed and
    the reader does not.
    """
    out: list[str] = []
    for raw_key, value in (call.arguments or {}).items():
        # Every string here was written by the model or returned by a tool it
        # called, so none of it is this client's text. The line structure is
        # kept — a query is meant to span lines — and the characters that
        # could repaint the terminal are not.
        key = safe_text(raw_key)
        # SQL arrives multi-line and stays multi-line; the continuation is
        # indented under its key so a long query still reads as one argument.
        lines = safe_block(value).strip().splitlines() or [""]
        out.append(f"{key}: {lines[0]}")
        out.extend(f"{' ' * (len(key) + 2)}{line}" for line in lines[1:])
    body = safe_block(call.result or "").splitlines()
    out.extend(f"→ {line}" for line in body[:max_result_lines])
    if len(body) > max_result_lines:
        out.append(f"→ … {len(body) - max_result_lines} more lines")
    return out


if __name__ == "__main__":
    raise SystemExit(main())
