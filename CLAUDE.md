# btc_dashboard — working context

BTC analytics. Four sources collapse into one JSON **snapshot**, which four
consumers read: a terminal panel, a self-contained HTML page, an LLM context
block, and raw JSON. Full design in `README.md` — read the *Design* and
*Measurement notes* sections before changing how any figure is computed or
displayed.

## Working agreement

**Propose before implementing. Wait for explicit approval.**

Before writing or changing anything, post:

1. **Observations** — what the relevant code actually does now, and anything
   found while looking that changes the shape of the request.
2. **Proposed approach** — the design, the files it touches, and the
   alternatives rejected, with the reason.
3. **Concerns** — constraints it strains, risks, and anything in the request
   that looks wrong from inside the code.

Then stop. Silence is not approval; neither is a question answered, nor an
earlier approval of something adjacent. Only an explicit go-ahead starts the
work. The point is room to refine the instruction while it is still cheap to
refine — before there is a diff to argue with.

Applies to code, tests, docs and config alike, and to a one-line change as much
as a large one. It does not apply to reading the repo, searching, running the
tests, or answering a question about how something works: the investigation
needed to write the proposal is part of the proposal.

Stated as a gate rather than as advice deliberately. The advisory version in
*Working practices* §1 was already in this file when ~1,200 lines of analyst
tooling arrived unannounced.

## Current state

- **Sources** (`src/btc_dashboard/sources/`): `price` (CoinGecko → Binance),
  `node` (`bitcoin-cli`), `warehouse` (DuckDB, read-only), `flows` (Farside
  scrape). Collected concurrently, each independently fail-soft.
- **Consumers**: `render.render()` terminal, `html.render_html()` page,
  `analyst.build_context()` LLM, `--json`. None of them re-fetch anything.
- **Cache**: 60-minute TTL for `warehouse` and `flows` only. `price` and `node`
  are live tip state and are never cached.
- **Analyst**: `--ask`, opt-in, client-side. Providers in `providers.py` —
  anthropic (default), openai, deepseek, openrouter, ollama. Tool use is
  supported on both wire protocols; the warehouse lends a read-only SQL tool,
  and `--no-tools` forces the old single-shot behaviour.
- **Web**: `btc-dashboard-web` — FastAPI on `127.0.0.1:8001`, ask box,
  `NOTABLE` strip, systemd unit in `deploy/`. Live on the Pi. The page patches
  its data regions from `/live` on a timer; it does not reload.
- **Studies**: `tools/hashrate_study.py`, read-only over the warehouse.

## Hard constraints

Breaking one of these is a regression even when the number is right.

- **The snapshot is the contract.** Nothing downstream collects or re-fetches.
  A new consumer reads the dict; it does not call a source.
  - The one exception, deliberate and narrow: a source may expose
    `analyst_tools(cfg)` returning `sources.Tool`s, which **only** `--ask` may
    call, and only while answering. A tool's result never enters the snapshot
    and no other consumer can reach it. Rendering is still a pure function of
    the dict.
- **`collect()` never raises.** A source that cannot produce data returns
  `available: false` with the reason. One dead source costs one block, never
  the run — and the analyst is *told* what is missing, so it reasons about the
  gap instead of assuming health.
- **The warehouse is opened `read_only=True`, always.** A separate ingester
  (see `data_stores`) is its sole writer. Nothing here writes to it, ever.
- **The analyst's SQL connection assumes the worst statement.** `read_only`
  alone is not enough: it stops writes to the *database*, not `read_csv` over
  the filesystem, `COPY ... TO`, `ATTACH`, or `INSTALL httpfs` reaching the
  network. `_connect_sandboxed` adds `enable_external_access=false` and
  `lock_configuration=true`. Statements are not pattern-matched — wrapping in
  `SELECT * FROM (...) LIMIT n` makes DuckDB's parser the authority on what is
  one read, so no blocklist has to anticipate the next statement type. Weaken
  any of that and a model's typo becomes a filesystem read.
- **`--ask` is client-side.** `analyst.py` must never be imported by a data
  plane that serves other people; a snapshot service holds no credential. The
  web view is the one documented exception — it holds the key, which is why it
  binds loopback and `/ask` has a cooldown.
- **Every figure carries its qualifier.** Percentile window (`2y` vs all
  history), volatility annualisation (`√365`), the date a close belongs to, the
  noise band on a one-day count. These are what make a number comparable to
  someone else's; dropping one to tidy a layout is the most common way to
  regress this project.
- **The ask box is never inside a region the page updates.** `html.LIVE_IDS`
  names what a tick overwrites; the box and the answer sit outside all of it.
  Reloading the document to refresh the data throws away a half-typed
  question, which is the one thing on the page the reader owns rather than the
  snapshot. `render_live()` therefore serves no form controls at all, and a
  test walks the page to prove the box has no live region as an ancestor.
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
def analyst_tools(cfg) -> list[Tool]:   # optional; live queries lent to --ask
def analyst_scope(data) -> str | None:  # optional; what that tool can reach
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
  not a bug: no `bitcoin-cli`, no DuckDB file. It also means `--ask` offers no
  query tool there, and is *told* so — a model that thinks it can check history
  and cannot will answer from the snapshot while sounding like it checked.
- **The DuckDB lockdown is instance-wide, not per-connection.** After one ask,
  every later connection to that file in the process is locked too, and
  `lock_configuration` cannot be undone. Harmless today because nothing here
  uses external access — but a `SET` added anywhere in `warehouse.py` would
  start failing only after an ask had run, which is a horrible bug to chase.

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
  anything is exposed beyond loopback. The web `/ask` now runs SQL as well as
  spending money, which raises the cost of getting that wrong. Load shedding is
  already handled by the cache.
- `README.md` is long; if it grows further, split *Measurement notes* and
  *Studies* into `DECISIONS.md`, as `data_stores` does.
- No long-history directional flow series exists. Adding one (exchange
  netflows, stablecoin issuance) would do more for the analysis than refining
  any existing measure.

## Working practices

Behavioral guidelines to reduce common LLM coding mistakes.

**Tradeoff:** these bias toward caution over speed, and for a trivial task that
is a poor trade — use judgment about *how much* of this to apply. Not about
whether to apply *Working agreement* above: the approval gate has no trivial
case, which is the whole reason it is stated separately from these.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

The hard version of this is *Working agreement* at the top of this file, which
requires explicit approval before implementation begins. What follows is how to
spend the time before that approval well.

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.
