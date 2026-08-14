# btc_dashboard

BTC-focused analytics. Collects live network state, deep on-chain history, spot
price against its 200-day SMA, and U.S. spot ETF flows into a **single JSON
snapshot**, renders it as a terminal panel, and — only when you ask — answers
ad-hoc questions about it through the LLM provider of your choice, using
credentials on your own machine.

```
$ btc-dashboard
BTC DASHBOARD — 2026-07-29 23:22:31 UTC
────────────────────────────────────────────────────────────
PRICE
  spot $63,666 (coingecko)
  SMA 20d $64,444 -1.2% | 50d $63,336 +0.5% | 200d $71,707 -11.2% (below 200d)

NETWORK (live)
  height 903,142 | hashrate 812.44 EH/s (+1.20% 7d) | difficulty 110.57T
  retarget 1,204 blks (~8.4d) | proj -1.83%
  mempool 14,203 tx / 8.1 vMB
  fees 4.0/2.3/0.7 sat/vB (fast/1hr/1d)

ON-CHAIN (last complete UTC day)
  day (UTC 2026-07-28 Tue): 127 blks | 97% full | p50 1.0 sat/vB | fee/subsidy 0.75% | miner rev 399.8 BTC
  signal: fee/subsidy 26th pctile 2y (7d) | apathy 22d | hashrate -16.0% off 90d high
  daily close $63,860 (warehouse)
  SMA 20d $64,420 -0.6% | 50d $65,020 -1.5% | 200d $71,862 -11.1%
  vol (ann √365, pctile 2y/all): 7d 16% (3/3) | 30d 28% (11/7) | 90d 34% (14/5) | 180d 38% (20/5) | 360d 43% (24/5)
  block pace 127/144 (-11.8%, ±8% day-to-day noise)

ETF FLOWS
  latest -49.7M total | -54.8M IBIT (28 Jul 2026, 1d ago)
  5d net -457.4M total | -439.5M IBIT (96% IBIT — conviction distribution)
  20d net -49.6M total | -135.3M IBIT
  60d net -6.74B total | -4.89B IBIT
  streak 4d outflow
```

```bash
btc-dashboard --ask "is the flow picture consistent with price below the 200d?"
```

---

## Design

**The snapshot is the contract.** Every consumer — the text renderer, the LLM
analyst, and the JSON service this is heading toward — reads the same dict and
nothing re-fetches a source:

```
       ┌─ price      (CoinGecko → Binance)
       ├─ node       (bitcoin-cli)                        ┌─► render  → terminal
sources┤                                    ├─► snapshot ─┼─► JSON    → --json / service
       ├─ warehouse  (DuckDB, read-only)                  └─► analyst → LLM    [--ask only]
       └─ flows      (Farside + cache)
```

Collection never touches an LLM. Only `--ask` does, and it runs on your machine
under your key — see [Credential boundary](#credential-boundary-the-llm-is-client-side-only).

**Every source is independently fail-soft.** `collect()` never raises; a source
that can't produce data returns `available: false` with the reason attached.
An unreachable node, a missing warehouse, or a Farside layout change costs one
block of the panel and leaves the rest intact — and the *analyst is told what is
missing*, so it reasons about a gap rather than assuming a healthy network.

**Standalone.** No imports from any sibling project, and no shelling out to
their scripts. The only shared thing is *data*: the DuckDB file, opened
read-only. Third-party libraries plus upstream `bitcoin-cli`, nothing else.

Sources are collected concurrently, so a run costs the slowest source rather
than the sum.

### Caching

On-chain and ETF flow data are cached for **60 minutes**; price and node are
never cached.

That split is the whole design. The warehouse gains one row per UTC day and
Farside publishes a trading day once, in the evening — so refetching either
more than hourly buys nothing and, for Farside, just adds load to someone
else's site. Spot price and mempool depth are live tip state, where serving a
40-minute-old reading as current would be worse than not showing it.

```
$ btc-dashboard --only flows      # cold
  1.80s
$ btc-dashboard --only flows      # warm
ETF FLOWS [cached 3m]
  0.07s
$ btc-dashboard --only flows --refresh   # forced re-collect
  1.10s
```

**`cached` and `stale` are different states**, and both appear in the snapshot:

| Marker | Meaning |
| --- | --- |
| *(none)* | Collected live this run |
| `[cached 15m]` | Served from disk within its TTL — as good as when fetched |
| `[STALE 3d]` | TTL expired *and* the live refresh failed; serving the old copy anyway, with the failure reason shown |

The stale path is why the cache is worth having beyond speed: when Farside is
unreachable, yesterday's finalized flows still render rather than the block
disappearing. The analyst is told which state applies, so it can't describe an
hour-old reading as "right now".

**Time-relative fields are recomputed on every cache read.** `age_days` and
`days_behind` are relative to when they are *read*, not when they were
fetched — otherwise a three-day-old stale payload would report a trading day
as "1d ago", exactly when accuracy matters most. A source with such fields
exposes `refresh_derived(data)`.

Cache files live in `~/.cache/btc_dashboard/<source>.json`. Writes are atomic
(temp file + `os.replace`), so concurrent readers never see
a partial payload. An unreadable or corrupt cache file is treated as a miss,
never an error.

### Adding a source

One new file in `sources/` exposing four names, plus one entry in
`snapshot.SOURCES`. Nothing else changes:

```python
NAME = "mysource"
CACHE_TTL = 3600                        # optional; omit for live data
def collect(cfg) -> SourceResult: ...   # never raises
def render_lines(data) -> list[str]:    # terminal text
def context_lines(data) -> list[str]:   # facts phrased for the LLM
def refresh_derived(data) -> dict:      # optional; only if fields age
```

Keeping both presentations next to the collector is deliberate: the caveats a
number needs ("this window is n/a, not zero") belong with the code that knows
why.

---

## Install

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
```

Then `btc-dashboard`, or `python -m btc_dashboard.cli`.

## Usage

```bash
btc-dashboard                          # full panel
btc-dashboard --json                   # the snapshot, for piping
btc-dashboard --only flows,price       # subset
btc-dashboard --context                # what the analyst would be told (no API call)
btc-dashboard --ask "QUESTION"         # send the snapshot to an LLM (local key, opt-in)
btc-dashboard --from URL|PATH|-        # ingest a snapshot instead of collecting one
```

`--ask` is the only thing that contacts an LLM, and it always runs locally —
see [Credential boundary](#credential-boundary-the-llm-is-client-side-only).

| Flag | Effect |
| --- | --- |
| `--json` | Emit the snapshot instead of the panel |
| `--from X` | Ingest a snapshot (http(s) URL, file, or `-`) instead of collecting one |
| `--only A,B` | Restrict to named sources (`price`, `node`, `warehouse`, `flows`) |
| `--ask Q` | Run the analyst over the snapshot |
| `--context` | Print the analyst's fact list and exit — use this to debug what it sees |
| `--db PATH` | Warehouse path |
| `--provider P` | LLM provider for `--ask` (`anthropic`, `openai`, `deepseek`, `openrouter`, `ollama`) |
| `--model ID` | Model for `--ask`, optionally `provider/model` |
| `--effort L` | `low`/`medium`/`high`/`xhigh`/`max` (default `high`) |
| `--refresh` | Bypass the cache and re-collect |
| `--cache-ttl N` | Cache lifetime in seconds (default 3600; `0` disables) |
| `--quiet` | Hide unavailable-source detail |

Exit codes: `0` ok, `1` no source available, `2` bad usage or analyst failed. The analyst
failing never costs you the panel — it prints first.

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
mkdir -p ~/.config/btc_dashboard && chmod 700 ~/.config/btc_dashboard
echo 'ANTHROPIC_API_KEY=sk-ant-...' > ~/.config/btc_dashboard/env
chmod 600 ~/.config/btc_dashboard/env
```

Paths follow the XDG base directory spec: cache under `$XDG_CACHE_HOME`
(default `~/.cache/btc_dashboard`), config under `$XDG_CONFIG_HOME` (default
`~/.config/btc_dashboard`). The pre-XDG `~/.btc_dashboard/env` is still read as
a fallback, since it may hold a key; the old cache directory is not — cache is
disposable, so an upgraded install simply refetches once. If you have one left
over, `rm -rf ~/.btc_dashboard` after moving any env file.

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

## Measurement notes

The numbers are the easy part; these four decisions are what make them mean
something. Each is enforced in code and covered by a test.

**A reported zero is not a missing cell.** Farside renders an unpublished figure
as `-` and a genuine zero flow as `0.0`. Collapsing them turns "hasn't reported"
into "reported no flow" and drags every average toward zero.

**A flow day counts only once every tracked fund has reported.** Funds post
progressively through the afternoon, and a day read mid-session has a real but
incomplete total whose *sign* can still flip. Partial days are excluded from
every figure and surfaced separately.

**An unfillable window reports `n/a`, never a shorter sum.** A 60-day net over
40 available days is a 40-day net wearing a 60-day label — worse than no answer,
because it looks like one. The analyst is told explicitly not to read it as zero.
The same rule governs the moving averages: with 60 days of history you get a
20d and a 50d SMA and `200d n/a`, never a 60-day mean labelled 200d.

**A tag names the window it came from.** The lead-share classifier describes
the *primary (5d) window*, so it is printed on that window's line where the
total's sign is visible beside it — not on the streak line, which is a
different measure that can point the other way. A one-day inflow inside a
five-day net outflow is ordinary. The label carries direction for the same
reason: "conviction" alone reads as conviction *buying*, so an outflow window
tagged with the bare word said the opposite of what the data meant.

**Weekly seasonality is corrected, two different ways.** `fee_subsidy` runs
materially lower at weekends, so a raw daily percentile substantially reports
the day of the week rather than the network. A 7-day mean spans one of each
weekday and cancels the cycle, but blurs single-day events; ranking against the
same weekday's mean keeps daily resolution. Sustained regimes get smoothing,
transient spikes get detrending. `tests/test_warehouse.py` pins this: on a purely
seasonal series the raw percentile swings ~28 points between a Wednesday and a
Saturday reading, and the smoothed one barely moves.

Related: percentiles are **mid-ranked with a floating-point tolerance**. The
sliding-window mean is computed incrementally, so mathematically-equal values
differ in their last bits — a strict comparison split a perfectly flat series
across the 58th percentile. Ties are now counted and halved, so a value equal to
everything else reads 50th.

**A single day's block count is noise, and is labelled as such.** Block
discovery is Poisson, so one day at the 144-block target has a standard
deviation of 12 blocks — about 8%. A day at -12% is only 1.4sd low, which
happens roughly one day in twelve by chance. It is therefore rendered as
`block pace 127/144 (-11.8%, ±8% day-to-day noise)` rather than as a second
"retarget projection" competing with the node's cumulative estimate, which is
computed over the whole difficulty period and is the number to trust for
direction.

**Volatility is reported as a level *and two* percentiles, with the
annualisation named.** The level is not portable: the same series on a 252-day year reads
~17% lower, and the price source and close time move it further. So a level
compared against someone else's published threshold silently compares
conventions as much as markets — a reading of 28% here and 18% elsewhere can
be the same market. The percentile travels; the level does not. Both are
shown, and `ann √365` is printed so a disagreement is diagnosable rather than
mysterious.

The two percentile windows — `2y/all` — exist because they disagree by up to
19 points. Bitcoin's volatility has declined structurally as the market
matured (median 30d vol: 79% in 2014, 38% in 2026), so ranking today against
the 2014–17 era substantially reports that decline rather than current
conditions. On the live series 360d vol reads **5th percentile of all history
and 24th of the last two years** — the first number is mostly about
maturation, the second about now. Short windows barely move (7d is 3rd
either way), so the divergence is concentrated exactly where the all-history
figure is least trustworthy. The 2-year window matches the one the
`signal:` line already uses, so "percentile" means the same thing on both
lines, and the analyst is told to prefer it.

At the extremes the percentile reports as a band (`<1`, `>99`) rather than a
rounded bound. A mid-ranked percentile can never actually reach 0 — the single
lowest of 730 observations ranks 0.07 — so printing `0` claimed an all-time
floor for what was the second-lowest reading of two years.

The estimates are close-to-close, because the warehouse holds no OHLC.
Range-based estimators (Parkinson, Garman-Klass) are several times more
efficient per observation and are simply unavailable here — worth knowing when
comparing against a vendor figure. There is no options data either, so this is
realised volatility only, never implied.

And the caveat that belongs next to the numbers: **volatility describes the
size of moves, not their direction.** Conditioning next-30d outcomes on the
current 30d vol quintile gives a U-shape in the *absolute* move — the lowest
and highest quintiles both precede larger moves than mid-range ones — while
the signed move barely separates. It is not a bottom indicator, and the
analyst is told so explicitly.

Absolute and relative thresholds are kept distinct on purpose. A percentile
recalibrates to the window it measures, so by construction only N% of days can
sit below the Nth percentile however depressed the regime — it finds a *new low*
and can never express the *duration* of a sustained one. That's why the apathy
streak uses a fixed threshold.

## The warehouse is read-only

A separate ingester owns writes to `market.duckdb`. DuckDB permits one writer
per file, and this tool opens it `read_only=True` everywhere — that is what
keeps it from ever contending with the writer. Nothing here writes, and nothing
here should.

### Choosing a provider

`--ask` is the only thing that contacts an LLM, and the provider is yours to
pick. `anthropic` is the default; `openai`, `deepseek` and `openrouter` speak
the OpenAI chat-completions shape; `ollama` runs locally and needs no key.

```bash
btc-dashboard --ask "..."                                   # anthropic default
btc-dashboard --ask "..." --model deepseek/deepseek-chat    # provider in the id
btc-dashboard --ask "..." --provider ollama --model llama3  # local, no key
```

A `provider/` prefix on `--model` beats `--provider`, so `BTC_DASHBOARD_MODEL`
can carry both in one variable for a scheduled run. Each provider reads its own
key (`ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, …) from the environment or the
env file.

**There is no config-level default model.** An unset model means "use the
chosen provider's default" — a shared default would be silently applied to
whichever provider was selected, so `--provider openai` would have requested an
Anthropic model id from OpenAI. Providers without a stable default (OpenAI,
OpenRouter) ask you to name one rather than guessing at an id that moves.

Only Anthropic receives `--effort`; the OpenAI-shaped path never sends it, and
`max_tokens` is omitted where a provider rejects it.

## Credential boundary: the LLM is client-side only

**The analyst never runs server-side.** This is the load-bearing rule of the
design, not an implementation detail:

```
   ┌──────────── service (data plane) ─────────┐   ┌──── local CLI ────┐
   │  sources → snapshot → raw JSON over HTTP  │──▶│ --from → render   │
   │  no API key · no model config · no egress │   │ --ask  → Claude   │
   └───────────────────────────────────────────┘   └──── your key ─────┘
```

- The service **collects and serves JSON, nothing else.** It holds no
  `ANTHROPIC_API_KEY`, imports no model client, and makes no outbound LLM call.
  There is no credential on it to steal and no way to make it spend yours.
- **`--ask` is opt-in and always local.** It reads the selected provider's key
  on the machine you run it from, and uses the provider, model and effort
  configured *there*. Two people can point at the same service and use
  different providers entirely — including a local one, where the snapshot
  never leaves the machine that collected it.
- `snapshot.py` and every source are LLM-free by construction; `analyst.py` is
  the only module that touches the API, and a service must not import it.

This holds today, before any server exists — `--from` is the client half:

```bash
# on the node host
btc-dashboard --json > /srv/www/btc/snapshot.json

# anywhere else — collection is remote, analysis is local
btc-dashboard --from https://pi.local/btc/snapshot.json --ask "what changed?"
btc-dashboard --json | ssh laptop 'btc-dashboard --from - --ask "..."'
```

`--from` accepts an http(s) URL, a file path, or `-` for stdin. Other schemes
are refused rather than guessed at.

### Ingested snapshots are untrusted

Once a payload can arrive over the wire it is untrusted input heading into a
prompt, and it's handled as such:

- `schema_version`, `sources`, and per-block shape are validated on ingest; a
  payload from a newer service is refused with "upgrade the client" rather than
  half-read.
- Free-text fields (`error`) are whitespace-collapsed and truncated before
  reaching the prompt, so an injected `\n\nIGNORE PRIOR INSTRUCTIONS…` can't
  forge its own section — it stays on one labelled data line.
- The context block is explicitly framed as data, and the system prompt tells
  the model to flag instruction-like content as an anomaly rather than obey it.
- A source this build has no renderer for is reported as present-but-
  uninterpretable; its raw contents are never dumped into the prompt as a guess.

### Still required before exposing it

Authentication, TLS, and rate limiting. **The cache/TTL layer is now built**
(see [Caching](#caching)) — N clients hitting the service share one hourly
Farside scrape and one hourly warehouse read rather than each triggering their
own. The rest is not built: don't expose it publicly until it is. The service
being read-only and secret-free limits the blast radius but does not
substitute for any of them.

## Studies

`tools/` holds one-off analyses that query the warehouse read-only. They are not
part of the CLI and nothing in the package imports them.

```bash
python tools/hashrate_study.py                    # full report
python tools/hashrate_study.py --min-depth 50     # macro bottoms only
python tools/hashrate_study.py --json
```

**`hashrate_study.py`** — do hashrate-derived indicators mark cycle price
bottoms? It derives drawdown episodes from the price series (rather than
hardcoding dates chosen with hindsight), scores the hash ribbon against the
*unconditional base rate*, and conditions forward returns on hashrate drawdown
across every day rather than on a handful of troughs.

Three reporting choices are deliberate, because each guards a way this kind of
study normally misleads:

- **A base-rate column.** BTC's unconditional forward return over the sample is
  strongly positive, so any signal judged on its own absolute return looks
  excellent. Only the edge over entering on a random day means anything — and
  the hash ribbon's edge is real at 90–180d and gone at 365d.
- **An effective-sample column.** Daily rows are autocorrelated and their
  forward windows overlap, so a decile of ~380 days holds barely one
  independent 365-day observation. Printing `n=386` alone would imply a
  precision the data cannot support.
- **Durations measured from the peak, not the trigger.** `start` is the day
  price crossed the threshold, typically days or weeks after the top;
  measuring from it understates every decline. The table reports peak → trough
  and trough → recovery separately, since the two halves are not symmetric,
  and shows elapsed-so-far for an episode still running.
- **A bounded signal-to-trough association.** An unbounded "nearest signal"
  always finds one; it matched the 2017 corrections to signals 200–390 days
  away. Beyond 90 days the tool reports none rather than inventing a link.

The headline finding is negative and worth keeping: hashrate stress was extreme
at two of four macro troughs (2018, 2021) and entirely ordinary at the other two
(2022, 2026). A credit or exchange failure can bottom price while hashrate barely
moves, so no single hashrate metric can confirm a bottom on its own.

## Tests

```bash
.venv/bin/pytest
```

263 tests. The warehouse tests run against a real DuckDB file built per-test
rather than a mock — the signal definitions are the part most worth pinning
down, and a mock would only assert that we called ourselves.

## License

[MIT](LICENSE) © 2026 Michael OConnor

## Disclaimer

Flow data is scraped from Farside Investors and provided as is, with no
guarantee of accuracy, completeness, or timeliness. Price data comes from public
APIs on the same terms. This is informational tooling and **not investment
advice**.
