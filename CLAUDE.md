# btc_dashboard — working context

BTC analytics. Four sources collapse into one JSON **snapshot**, which four
consumers read: a terminal panel, a self-contained HTML page, an LLM context
block, and raw JSON. Full design in `README.md` — read the *Design* and
*Measurement notes* sections before changing how any figure is computed or
displayed.

## Current state

- **Sources** (`src/btc_dashboard/sources/`): `price` (CoinGecko → Binance),
  `node` (`bitcoin-cli`), `warehouse` (DuckDB, read-only), `flows` (Farside
  scrape). Collected concurrently, each independently fail-soft.
- **Consumers**: `render.render()` terminal, `html.render_html()` page,
  `analyst.build_context()` LLM, `--json`. None of them re-fetch anything.
- **Cache**: 60-minute TTL for `warehouse` and `flows` only. `price` and `node`
  are live tip state and are never cached.
- **Analyst**: `--ask`, opt-in, client-side. Providers in `providers.py` —
  anthropic (default), openai, deepseek, openrouter, ollama.
- **Web**: `btc-dashboard-web` — FastAPI on `127.0.0.1:8001`, ask box,
  `NOTABLE` strip, systemd unit in `deploy/`. Live on the Pi.
- **Studies**: `tools/hashrate_study.py`, read-only over the warehouse.

## Hard constraints

Breaking one of these is a regression even when the number is right.

- **The snapshot is the contract.** Nothing downstream collects or re-fetches.
  A new consumer reads the dict; it does not call a source.
- **`collect()` never raises.** A source that cannot produce data returns
  `available: false` with the reason. One dead source costs one block, never
  the run — and the analyst is *told* what is missing, so it reasons about the
  gap instead of assuming health.
- **The warehouse is opened `read_only=True`, always.** A separate ingester
  (see `data_stores`) is its sole writer. Nothing here writes to it, ever.
- **`--ask` is client-side.** `analyst.py` must never be imported by a data
  plane that serves other people; a snapshot service holds no credential. The
  web view is the one documented exception — it holds the key, which is why it
  binds loopback and `/ask` has a cooldown.
- **Every figure carries its qualifier.** Percentile window (`2y` vs all
  history), volatility annualisation (`√365`), the date a close belongs to, the
  noise band on a one-day count. These are what make a number comparable to
  someone else's; dropping one to tidy a layout is the most common way to
  regress this project.
- **Meaning never lives in the presentation layer.** Strip the ANSI codes or
  the `<style>` block and the output must still say the same thing. Tests
  enforce this. It has been broken three separate ways — colour, a CSS-injected
  glyph, and flex `gap` supplying the only spacing.
- **An unfillable window reports `n/a`.** Never a shorter mean or sum wearing a
  longer label. Applies to flow windows, SMAs and volatility alike.
- **A reported zero is not a missing value.** Farside renders unpublished as
  `-`; only an explicit `0` is `0.0`.
- **No imports from sibling projects, no shelling out to their scripts.** The
  only shared thing is the DuckDB *file*.
  - The cost of that rule: the Farside scrape lives in **two** places, here and
    in `farside/farside_flows.py`. A layout change on their site breaks both
    separately — each has its own `parse_table`, column map and date regex — and
    the host running both scrapes the site twice. Deliberate: farside covers
    BTC/ETH/SOL and feeds the morning brief, this is BTC-only. If you fix a
    parsing bug here, check whether the other needs it too.

## Source contract

One new file in `sources/`, one entry in `snapshot.SOURCES`:

```python
NAME = "mysource"
CACHE_TTL = 3600                        # optional; omit for live tip state
def collect(cfg) -> SourceResult: ...   # never raises
def render_lines(data) -> list[str]:    # terminal
def context_lines(data) -> list[str]:   # facts phrased for the LLM
def html_panels(data) -> list[Panel]:   # optional; cards, each with a priority
def notable(data) -> list[str]:         # optional; NOTABLE strip, threshold-selected
def refresh_derived(data) -> dict:      # optional; only if fields age with the clock
```

All presentations live beside the collector on purpose: the caveat a number
needs belongs with the code that knows why.

## Non-obvious facts that have already cost time

- **The two price providers date a daily bar oppositely.** CoinGecko's points
  are instants at 00:00 UTC, so the one labelled 16 Aug is the *close of 15
  Aug*. A Binance kline is stamped with a day's open and carries that day's
  close.
- **ETF flow dates are U.S. trading days**, so age is measured in
  `America/New_York`. UTC runs ahead and reports yesterday's flows as "2d ago".
- **The warehouse is UTC-day bucketed and only stores finished days**, so it is
  structurally ≥1 day behind today. `days_behind` measures against the last
  *complete* day, not today.
- **Percentiles are mid-ranked with an epsilon.** DuckDB evaluates a sliding
  `avg()` incrementally, so mathematically equal values differ in their last
  bits; a strict `<` ranked a flat series at the 58th percentile.
- **Volatility annualises on √365, not √252.** The difference is ~17% — enough
  to move a reading across a published threshold.
- **On the Mac, `node` and `warehouse` report unavailable.** That is correct,
  not a bug: no `bitcoin-cli`, no DuckDB file.

## Data and hosts

- **Warehouse**: `~/data/market.duckdb`, Pi only. Written by `data_stores`'
  ingester on a 02:00 UTC timer. Override with `BTC_DASHBOARD_DB`.
- **Node**: `bitcoin-cli`, Pi only.
- **Cache**: `~/.cache/btc_dashboard/` (XDG). Disposable.
- **Env file**: `~/.config/btc_dashboard/env`, chmod 600 — holds the provider
  key, and sets *any* `BTC_DASHBOARD_*` variable.
- **Pi** is `pibot` over ssh. Non-interactive ssh has no `~/.local/bin` on
  `PATH`, so use `ssh pibot '~/.local/bin/btc-dashboard --json'` or
  `bash -lc`. Reach the web views with
  `ssh -L 8000:localhost:8000 -L 8001:localhost:8001 pibot`
  (8000 is `bitcoin_peer_monitor`).

## Environment

- Editable install, so **`git pull` alone deploys** — re-run `pip install -e .`
  only when dependencies, console scripts or packages change. The console
  script in `~/.local/bin` is a stub whose mtime never changes; it is not a
  staleness signal.
- **Mac (dev)**: venv at `.venv/`. Homebrew Python is PEP 668
  externally-managed — never `--break-system-packages` here.
- **Pi**: `pip install -e . --break-system-packages` (single-purpose
  appliance). Python 3.11.
- Tests: `.venv/bin/pytest`. `tests/conftest.py` isolates the cache directory,
  the environment and colour, so a run cannot depend on the developer's machine
  or leak into it.

## Conventions

- Python 3.11+, `src/` layout, pytest.
- Comments carry the *why*, especially where a plausible simpler version is
  wrong. Test names and docstrings state the failure being guarded.
- Prefer a threshold or a window in a named constant with its reasoning over a
  magic number.

## Next

- The JSON service still needs **authentication, TLS and rate limiting** before
  anything is exposed beyond loopback. Load shedding is already handled by the
  cache.
- `README.md` is long; if it grows further, split *Measurement notes* and
  *Studies* into `DECISIONS.md`, as `data_stores` does.
- No long-history directional flow series exists. Adding one (exchange
  netflows, stablecoin issuance) would do more for the analysis than refining
  any existing measure.
