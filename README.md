# btc_dashboard

BTC-focused analytics. Collects live network state, deep on-chain history, spot
price against its 200-day SMA, and U.S. spot ETF flows into a **single JSON
snapshot**, renders it as a terminal panel, and — only when you ask — answers
ad-hoc questions about it through the LLM provider of your choice, using
credentials on your own machine.

```
$ btc-dashboard
BTC DASHBOARD — 2026-08-16 20:52:08 UTC
────────────────────────────────────────────────────────────
PRICE
  spot $62,995 -0.06% vs 15 Aug close (coingecko)
  SMA 20d $63,842 -1.3% | 50d $63,516 -0.8% | 200d $69,353 -9.2% (below 200d)

NETWORK (live)
  height 962,780 | hashrate 911.41 EH/s (+1.37% 7d) | difficulty 127.48T
  retarget 868 blks, ~6.1d | proj -0.91%
  mempool 13,447 tx / 2.3 vMB
  fees 1.2/1.0/0.6 sat/vB (fast/1hr/1d)

ON-CHAIN (daily) [cached 37m]
  day (UTC 2026-08-15 Sat): 145 blks | 98% full | p50 1.0 sat/vB | fee/subsidy 0.51% | miner rev 455.5 BTC
  signal: fee/subsidy 33rd pctile 2y (7d) | apathy 3d | hashrate -12.6% off 90d high
  daily close $63,024 (warehouse)
  SMA 20d $63,829 -1.3% | 50d $63,509 -0.8% | 200d $69,373 -9.2%
  vol (ann √365, pctile 2y/all): 7d 10% (<1/1) | 30d 22% (<1/1) | 90d 34% (13/5) | 180d 38% (20/5) | 360d 43% (21/4)
  block pace 145/144 (+0.7%, ±8% day-to-day noise)

ETF FLOWS (US SPOT) [cached 37m]
  latest -56.2M total | -55.5M IBIT (14 Aug 2026, 2d ago)
  5d net -385.2M total | -78.9M IBIT (20% IBIT — broad distribution)
  20d net +452.5M total | +606.0M IBIT
  60d net -5.55B total | -3.91B IBIT
  streak 3d outflow
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

**Standalone.** No imports from any sibling project and no shelling out to
their scripts — third-party libraries plus upstream `bitcoin-cli`, nothing else.
See [Related repositories](#related-repositories) for what it does share.

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

### Colour

Section headers, the rule, and the cache markers are colourised **only when
writing to a terminal**, so `--json`, a redirect, or a pipe stay clean. The
`NO_COLOR` convention is honoured (any value disables), `FORCE_COLOR` forces it
on, and `--color always|never` overrides both.

Only the basic 8 ANSI colours are used, never 256-colour or truecolour: a
terminal maps those through the user's own theme, so they stay legible on light
and dark backgrounds alike. **No colour carries meaning on its own** — a
`[STALE]` marker reads identically in plain text, and stripping the escape
codes from coloured output reproduces the plain output exactly (there's a test
for that).

### HTML

`--html` renders the snapshot as a self-contained page — inline CSS, no
external assets, scripts or fonts — so it works from `file://`, from a static
server, or over an SSH tunnel with no internet access. Light and dark follow
`prefers-color-scheme`. The tab icon is inline in two formats — an SVG and a
32×32 PNG rasterised from the same rectangles — so it costs no request and the
page's "references nothing external" property stays literally checkable. Both
are offered because Safari has never read an SVG favicon from a data URI and
silently falls back to its own generated letter tile.

Each source contributes `html_panels()` beside its terminal and LLM
presentations, so a source that naturally splits (facts, signals, volatility)
says so itself rather than the page imposing a layout. Each panel declares a
`priority`, because the useful order isn't source order — volatility comes from
the warehouse but belongs beside price, since a distance from a moving average
only means something in volatility units.

**A `NOTABLE` strip leads the page**, listing readings extreme enough to lead
with, inline and pipe-separated so it stays one line and doesn't push the cards
it introduces below the fold. Two rules keep it honest:

- **Threshold-selected, not hand-picked.** Each source owns its own bounds via
  `notable()`, because what counts as extreme is a property of the measure. The
  strip is *absent* on an ordinary day — one that always finds three things to
  say teaches you to stop reading it.
- **Facts, never forecasts.** "30d volatility 22% — <1 pctile of 2y" is a
  reading with its window attached. "Compression, expect a large move" is a
  prediction, and volatility carries no direction. The reader draws the
  conclusion; there's a test asserting the words don't appear.

Volatility bounds fire at *both* tails, since historically the lowest and
highest quintiles each preceded larger moves than mid-range ones. Unavailable
and stale sources lead the strip, being facts about the snapshot rather than
about any one source.

**Every close names its day.** The price card compares spot against the last
completed daily close; the on-chain card shows the newest close the *warehouse*
holds. Whenever the ingester hasn't run yet those are different days, and two
undated "previous closes" a day apart look like the sources disagreeing about
the price rather than an ordinary one-day lag. The price card names its
reference date, and the on-chain day moved into that card's heading — as a row
it read as one metric among many rather than as the date of everything below it.

The two providers date a daily bar by opposite conventions, which is handled
explicitly: CoinGecko's points are instants stamped 00:00 UTC, so the one
labelled 16 Aug is the *close of 15 Aug*; a Binance kline stamped with a day's
open carries that day's close. Reading either the other way dates every close
a day out.

**Spot is coloured against the previous completed daily close**, with a dead
band: a move under 0.1% is left uncoloured, because one standard deviation of
a current day is near 1% and painting a tenth of that green asserts a
direction the number doesn't carry. The reference close comes from the price
source's *own* series, never the warehouse's — those are different venues, and
mixing them would fold a venue spread into a figure meant to show the day's
move. It is labelled "vs prev close" rather than "24h", because the reference
is the last finished day, which may be an hour or a day old.

**Colour lands on whatever is actually signed.** A 20-day SMA of $63,955
painted red because spot sits below it reads as "the average fell" — what is
negative is the *relationship*, which lives in the note. `Metric` therefore
carries `tone` and `note_tone`, and a row uses whichever one describes a signed
quantity. Same for hashrate: the level is not negative, its 7-day change is.
Where the value *is* the signed thing (a retarget projection, a flow total) the
value keeps the colour.

**Every card of a source carries its freshness badge**, not just the first. One
source can produce several cards — the warehouse yields on-chain, signals and
volatility — and the grid wraps them onto different rows, so a badge on the
first alone leaves the rest looking undated. The failure *reason* still appears
once, since repeating one error three times reads as three problems.

**Type is sized for reading, not for density.** The page sets no pixel base, so
it inherits the browser's own default and a reader who has already turned that
up gets it. Labels and prose use a UI face; only the figures use monospace,
where the fixed advance width earns its place aligning columns. Monospace
everywhere reads poorly at small sizes, and bold monospace on a dark background
worst of all.

**Qualifiers survive the move.** A row-based layout invites dropping the window
a percentile was ranked against, or the annualisation behind a volatility
figure, because the numbers look tidier without them — but those are exactly
what makes a figure comparable to an external source. Every `Metric` carries a
`note` and the note is rendered. Free text is HTML-escaped, since an ingested
snapshot's error strings are controlled by whoever produced it.

### Local web view

```bash
pip install -e ".[web]"
btc-dashboard-web                 # http://127.0.0.1:8001
```

Port **8001**, not 8000 — [bitcoin_peer_monitor](https://github.com/mikeoc61/bitcoin_peer_monitor)
conventionally takes 8000, and two local dashboards on one host shouldn't fight
over a port by default. To
tunnel both from a laptop:

```bash
ssh -L 8000:localhost:8000 -L 8001:localhost:8001 pibot
```

A taken port fails with a message naming the likely culprit and the flag to
fix it, rather than uvicorn's bare `[Errno 98]`.

To keep it running, `deploy/systemd/btc-dashboard-web.service` is a unit that
binds loopback, runs the console script rather than `uvicorn` directly (so the
safe defaults and the port check still apply), and keeps the API key out of the
unit file — `systemctl show` prints a unit's environment in full. See
[`deploy/README.md`](deploy/README.md).

Same page as `--html`, plus an **ask box** wired to the analyst. A question is
a form POST that redirects back to `/`, so reloading never re-submits and the
page's auto-refresh keeps working.

**This process holds your provider key**, which is a deliberate departure from
the boundary that holds everywhere else — see
[Credential boundary](#credential-boundary-the-llm-is-client-side-only). An ask
box in a browser cannot work any other way: the server has to make the call.
That is fine when the server *is* your own machine reached over a tunnel, which
is why:

- **the default bind is `127.0.0.1`.** On `0.0.0.0` anyone who can reach the
  port can spend your API budget. A wider bind prints a warning naming the SSH
  alternative.
- **`/ask` has a cooldown**, so a double-submit costs one call, not two.
- **every answer shows its token count**, so the cost is visible.

Collection is decoupled from HTTP: the app holds a snapshot in memory with a
short TTL, so an auto-refreshing tab doesn't scrape Farside once a minute and
three questions cost three LLM calls and zero collections. `refresh data` on
the page forces a re-collect.

### Adding a source

One new file in `sources/` exposing four names, plus one entry in
`snapshot.SOURCES`. Nothing else changes:

```python
NAME = "mysource"
CACHE_TTL = 3600                        # optional; omit for live tip state

def collect(cfg) -> SourceResult: ...   # never raises
def render_lines(data) -> list[str]:    # terminal text
def context_lines(data) -> list[str]:   # facts phrased for the LLM

def html_panels(data) -> list[Panel]:   # optional; cards for --html and the web view
def notable(data) -> list[str]:         # optional; entries for the NOTABLE strip
def refresh_derived(data) -> dict:      # optional; only if fields age with the clock
```

Keeping all three presentations next to the collector is deliberate: the caveats
a number needs ("this window is n/a, not zero") belong with the code that knows
why, and a page or a prompt assembled elsewhere is where they get dropped.

---

## Related repositories

Two sibling projects sit behind this one. **Neither is a dependency** — nothing
here imports them and nothing shells out to them.

| Repo | Relationship |
| --- | --- |
| [data_stores](https://github.com/mikeoc61/data_stores) | Shares *data*. Its `market_warehouse` ingester is the sole writer of `~/data/market.duckdb`; this reads the same file `read_only=True`. Every on-chain figure, moving average and volatility window here comes from that file. |
| [farside](https://github.com/mikeoc61/farside) | Shares *design*. The standalone ETF-flow scraper `sources/flows.py` was reimplemented from — same site, same hard-won semantics (a reported zero is not a missing cell; a day counts only once every tracked fund reports; an unfillable window is `n/a`, never a shorter sum), independent code and its own cache. Kept separate deliberately: farside covers BTC, ETH and SOL and feeds the morning brief, while this is BTC-only. |
| [bitcoin_peer_monitor](https://github.com/mikeoc61/bitcoin_peer_monitor) | Unrelated to the data, but shares a host and a pattern — a FastAPI page on loopback reached over an SSH tunnel. It conventionally holds port 8000, which is why this defaults to 8001. |

The split matters for the warehouse in particular: sharing the *file* rather
than the *package* means a schema change is the only thing that can break this
project, and the coupling is confined to one module (`sources/warehouse.py`), so
the store is swappable without touching anything else. It never writes, so a
running ingester is unaffected.

⚠️ **The Farside scrape exists in two places.** A change to Farside's table
layout breaks `farside_flows.py` and `sources/flows.py` *separately* — each has
its own `parse_table`, column mapping and date regex, and fixing one will not
fix the other. Both also scrape the same site on their own schedule, so the host
running both makes two requests where one would do. That is the accepted price
of keeping this project standalone; it is not an oversight.

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
| `--html` | Emit a self-contained HTML page instead of the panel |
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
| `--timeout N` | Per-source network timeout in seconds (default 20) |
| `--color C` | `auto` (default, terminal only) / `always` / `never` |
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

Authentication, TLS, and rate limiting — none of which exist. Don't expose it
publicly until they do; being read-only and secret-free limits the blast radius
but substitutes for none of them.

Load shedding is handled: the [cache](#caching) means N clients share one hourly
Farside scrape and one hourly warehouse read rather than each triggering their
own.

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

Four reporting choices are deliberate, because each guards a way this kind of
study normally misleads:

- **A base-rate column.** BTC's unconditional forward return over the sample is
  strongly positive, so any signal judged on its own absolute return looks
  excellent. Only the edge over entering on a random day means anything — and
  the hash ribbon's edge is real at 90–180d and gone at 365d.
- **An effective-sample column.** Daily rows are autocorrelated and their
  forward windows overlap, so a decile of ~380 days holds barely one
  independent 365-day observation. Printing `n=386` alone would imply a
  precision the data cannot support.
- **Durations measured from the peak.** Timing an episode from the day price
  crossed the threshold, rather than from the high it fell from, understates
  every decline by days or weeks. Peak → trough and trough → recovery are
  reported separately since the halves are not symmetric, and an unresolved
  episode shows elapsed-so-far rather than a blank.
- **A bounded signal-to-trough association.** An unbounded "nearest signal"
  always finds one, however far away — beyond 90 days the tool reports none
  rather than inventing a link.

The headline finding is negative and worth keeping: hashrate stress was extreme
at two of four macro troughs (2018, 2021) and entirely ordinary at the other two
(2022, 2026). A credit or exchange failure can bottom price while hashrate barely
moves, so no single hashrate metric can confirm a bottom on its own.

## Tests

```bash
.venv/bin/pytest
```

The warehouse tests run against a real DuckDB file built per-test rather than a
mock — the signal definitions are the part most worth pinning down, and a mock
would only assert that we called ourselves. `tests/conftest.py` isolates the
cache directory, the environment and colour, so a run cannot depend on the
developer's machine or leak into it.

## License

[MIT](LICENSE) © 2026 Michael OConnor

## Disclaimer

Flow data is scraped from [Farside Investors](https://farside.co.uk/) and
provided as is, with no
guarantee of accuracy, completeness, or timeliness. Price data comes from public
APIs on the same terms. This is informational tooling and **not investment
advice**.
