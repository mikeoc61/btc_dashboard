# Usage and configuration

[Back to the README](../README.md) · [Design and measurement reference](reference.md)

## Usage

```bash
btc-dashboard                          # full panel
btc-dashboard --json                   # the snapshot, for piping
btc-dashboard --only flows,price       # subset
btc-dashboard --context                # what the analyst would be told (no API call)
btc-dashboard --ask "QUESTION"         # send the snapshot to an LLM (local key, opt-in)
btc-dashboard --from URL|PATH|-        # ingest a snapshot instead of collecting one
```

In the CLI, only `--ask` contacts an LLM, using credentials on the invoking machine —
see [Credential boundary](reference.md#credential-boundary-the-llm-is-client-side-only).

| Flag | Effect |
| --- | --- |
| `--json` | Emit the snapshot instead of the panel |
| `--html` | Emit a self-contained HTML page instead of the panel |
| `--from X` | Ingest a snapshot (http(s) URL, file, or `-`) instead of collecting one |
| `--only A,B` | Restrict to named sources (`price`, `node`, `warehouse`, `flows`) |
| `--ask Q` | Run the analyst over the snapshot |
| `--context` | Print the analyst's fact list and exit — use this to debug what it sees |
| `--db PATH` | Warehouse path |
| `--provider P` | LLM provider for `--ask` (`anthropic`, `openai`, `deepseek`, `openrouter`, `ollama`) |
| `--model ID` | Model for `--ask`, optionally `provider/model` |
| `--effort L` | `low`/`medium`/`high`/`xhigh`/`max` (default `high`) |
| `--no-tools` | Answer from the snapshot alone — don't let `--ask` query the warehouse |
| `--refresh` | Bypass the cache and re-collect |
| `--cache-ttl N` | Cache lifetime in seconds (default 3600; `0` disables) |
| `--timeout N` | Per-source network timeout in seconds (default 20) |
| `--color C` | `auto` (default, terminal only) / `always` / `never` |
| `--quiet` | Hide unavailable-source detail |

Exit codes: `0` ok, `1` no source available, `2` bad usage or analyst failed. The analyst
failing never costs you the panel — it prints first.

While `--ask` waits, a spinner and an elapsed-seconds counter sit on one line
of stderr, erased when the answer prints. Off a terminal nothing is drawn at
all, so a redirect or a pipe stays clean — and a call that returns quickly
never paints, rather than flashing a spinner up and wiping it.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BTC_DASHBOARD_DB` | `~/data/market.duckdb` | Warehouse path (falls back to `MARKET_WAREHOUSE_DB`) |
| `BTC_DASHBOARD_BITCOIN_CLI` | `bitcoin-cli` | Path to the Core CLI |
| `BTC_DASHBOARD_CACHE` | `~/.cache/btc_dashboard` | Cache directory (honours `XDG_CACHE_HOME`) |
| `BTC_DASHBOARD_PROVIDER` | `anthropic` | Analyst provider |
| `BTC_DASHBOARD_MODEL` | provider's default | Analyst model, optionally `provider/model` |
| `BTC_DASHBOARD_EFFORT` | `high` | Analyst reasoning effort |
| `BTC_DASHBOARD_TIMEOUT` | `20` | Per-source network timeout (s) |
| `BTC_DASHBOARD_CACHE_TTL` | `3600` | Cache lifetime for cached sources (s) |
| `ANTHROPIC_API_KEY` etc. | — | The selected provider's key; required for `--ask` |
| `BTC_DASHBOARD_ENV` | `~/.config/btc_dashboard/env` | Path to the env file (honours `XDG_CONFIG_HOME`) |

### The env file

A scheduled run starts without a login shell, so nothing from your profile is
exported. The env file covers that case, and it sets **any** variable in the
table above — not only API keys:

```
# ~/.config/btc_dashboard/env
BTC_DASHBOARD_PROVIDER=openai
BTC_DASHBOARD_MODEL=openai/gpt-5.6-luna
BTC_DASHBOARD_EFFORT=medium
OPENAI_API_KEY=sk-...
```

A variable already set in the real environment wins, so an explicit `export`
or a one-off `BTC_DASHBOARD_MODEL=x btc-dashboard` still overrides the file.
`export` prefixes, quotes, comments and blank lines are all tolerated; nothing
in the file is executed, so it can only set variables.

Create it with restrictive permissions, since it may hold a key:

```bash
mkdir -p ~/.config/btc_dashboard
chmod 700 ~/.config/btc_dashboard
touch ~/.config/btc_dashboard/env
chmod 600 ~/.config/btc_dashboard/env
# Edit this file to add your provider settings and key.
```

Paths follow the XDG base directory spec: cache under `$XDG_CACHE_HOME`
(default `~/.cache/btc_dashboard`), config under `$XDG_CONFIG_HOME` (default
`~/.config/btc_dashboard`). The pre-XDG `~/.btc_dashboard/env` is still read as
a fallback, since it may hold a key; the old cache directory is not — cache is
disposable, so an upgraded install simply refetches once. Keep any credentials
when migrating from the old directory.

## Where it runs

| Source | Needs | Laptop | Node host |
| --- | --- | --- | --- |
| `price` | network | ✅ | ✅ |
| `flows` | network | ✅ | ✅ |
| `node` | `bitcoin-cli`, synced node | ❌ | ✅ |
| `warehouse` | the DuckDB file | ❌ | ✅ |

On a laptop you get price and flows and an explicit note about the other two,
which is enough to develop against. Full fidelity needs the node host.

### Deploying to the node host

```bash
git clone https://github.com/mikeoc61/btc_dashboard.git
cd btc_dashboard
pip install -e . --break-system-packages    # single-purpose appliance
btc-dashboard                                # all four sources should report
```

`--break-system-packages` is for a dedicated appliance where the system Python
*is* the environment. On any machine you also use for other things, prefer a
venv (`python3 -m venv .venv && .venv/bin/pip install -e .`).

Two things to confirm on first run there, because they're the sources a laptop
can't exercise:

- `node` — needs `bitcoin-cli` on `PATH` and a synced node. If the daemon runs
  as another user, set `BTC_DASHBOARD_BITCOIN_CLI` to a wrapper carrying the
  right `-datadir`/`-conf`.
- `warehouse` — needs the DuckDB file. Set `BTC_DASHBOARD_DB` if it isn't at
  `~/data/market.duckdb`. Opened read-only, so it can run while the ingester
  writes.

If you want the analyst on a schedule there, the key must be in the env file —
a timer starts without a login shell, so nothing from your profile is exported.
See [Configuration](#configuration).

---
