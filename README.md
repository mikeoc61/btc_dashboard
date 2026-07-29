# btc_dashboard

BTC-focused analytics. Collects live network state, deep on-chain history, spot
price against its 200-day SMA, and U.S. spot ETF flows into a **single JSON
snapshot**, renders it as a terminal panel, and — only when you ask — answers ad-hoc
questions about it through Claude, using credentials on your own machine.

```
$ btc-dashboard
BTC DASHBOARD — 2026-07-29 23:22:31 UTC
────────────────────────────────────────────────────────────
PRICE
  spot $63,960 (coingecko)
  200d SMA $71,865 | -11.0% (below)

NETWORK (live)
  height 903,142 | hashrate 812.44 EH/s (+1.20% 7d) | difficulty 110.57T
  retarget 1,204 blks (~8.4d) | proj -1.83%
  mempool 14,203 tx / 8.1 vMB
  fees 2/2/1 sat/vB (fast/1hr/1d)

ON-CHAIN (last complete UTC day)
  day (UTC 2026-07-28 Tue): 147 blks | 96% full | p50 2.0 sat/vB | fee/subsidy 0.84% | miner rev 452.1 BTC
  signal: fee/subsidy 3rd pctile 2y (7d) | apathy 41d

ETF FLOWS
  latest -49.7M total | -54.8M IBIT (28 Jul 2026, 1d ago)
  5d net -457.4M total | -439.5M IBIT
  20d net -49.6M total | -135.3M IBIT
  60d net -6.74B total | -4.89B IBIT
  streak 4d outflow — conviction
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
       ├─ warehouse  (DuckDB, read-only)                  └─► analyst → Claude  [--ask only]
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

### Adding a source

One new file in `sources/` exposing four names, plus one entry in
`snapshot.SOURCES`. Nothing else changes:

```python
NAME = "mysource"
def collect(cfg) -> SourceResult: ...   # never raises
def render_lines(data) -> list[str]:    # terminal text
def context_lines(data) -> list[str]:   # facts phrased for the LLM
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
btc-dashboard --ask "QUESTION"         # send the snapshot to Claude (local key, opt-in)
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
| `--model ID` | Model for `--ask` |
| `--effort L` | `low`/`medium`/`high`/`xhigh`/`max` (default `high`) |
| `--quiet` | Hide unavailable-source detail |

Exit codes: `0` ok, `1` no source available, `2` bad usage or analyst failed. The analyst
failing never costs you the panel — it prints first.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `BTC_DASHBOARD_DB` | `~/data/market.duckdb` | Warehouse path (falls back to `MARKET_WAREHOUSE_DB`) |
| `BTC_DASHBOARD_BITCOIN_CLI` | `bitcoin-cli` | Path to the Core CLI |
| `BTC_DASHBOARD_CACHE` | `~/.btc_dashboard/cache` | Flow-cache directory |
| `BTC_DASHBOARD_MODEL` | `claude-opus-5` | Analyst model |
| `BTC_DASHBOARD_EFFORT` | `high` | Analyst reasoning effort |
| `BTC_DASHBOARD_TIMEOUT` | `20` | Per-source network timeout (s) |
| `ANTHROPIC_API_KEY` | — | Required for `--ask` |
| `BTC_DASHBOARD_ENV` | `~/.btc_dashboard/env` | Env file read when the key isn't exported |

A scheduled run starts without a login shell, so nothing from your profile is
exported. Put the key in the env file for that case:

```bash
mkdir -p ~/.btc_dashboard && chmod 700 ~/.btc_dashboard
echo 'ANTHROPIC_API_KEY=sk-ant-...' > ~/.btc_dashboard/env
chmod 600 ~/.btc_dashboard/env
```

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
- **`--ask` is opt-in and always local.** It reads `ANTHROPIC_API_KEY` on the
  machine you run it from and uses the model and effort configured *there*. Two
  people can point at the same service and use different models.
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

Authentication, TLS, rate limiting, and a cache/TTL layer so N clients don't
each trigger a Farside scrape. Not built. Don't expose it publicly until they
are — note the service is read-only and secret-free, which limits the blast
radius but is not a substitute for any of the above.

## Tests

```bash
.venv/bin/pytest
```

55 tests. The warehouse tests run against a real DuckDB file built per-test
rather than a mock — the signal definitions are the part most worth pinning
down, and a mock would only assert that we called ourselves.

## License

[MIT](LICENSE) © 2026 Michael OConnor

## Disclaimer

Flow data is scraped from Farside Investors and provided as is, with no
guarantee of accuracy, completeness, or timeliness. Price data comes from public
APIs on the same terms. This is informational tooling and **not investment
advice**.
