# btc_dashboard

A Bitcoin dashboard for the terminal and browser. See price trends, network
activity, on-chain history, and U.S. spot ETF flows together. Optionally ask an
LLM questions about the readings and your local historical data.

**Start with price and ETF flows on any machine with internet access.** Add a
Bitcoin Core node and a DuckDB warehouse for the full picture. A missing source
is shown as unavailable; the rest of the dashboard still works.

[Quick start](#quick-start) · [Browser dashboard](#browser-dashboard) ·
[Ask a question](#ask-a-question) · [Configuration](docs/usage.md#configuration) ·
[Technical reference](docs/reference.md)

## Quick start

Requires **Python 3.11 or newer**. No LLM key is needed to view the dashboard.

```bash
git clone https://github.com/mikeoc61/btc_dashboard.git
cd btc_dashboard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
btc-dashboard
```

To show only the sources that need internet access:

```bash
btc-dashboard --only price,flows
```

### What you get

| Section | Shows | Needs |
| --- | --- | --- |
| Price | Spot price, moving averages, and RSI momentum | Public price APIs |
| Network | Block height, hashrate, fees, mempool, and difficulty adjustment | `bitcoin-cli` connected to your node |
| On-chain | Daily network activity, historical signals, and realized volatility | A local DuckDB warehouse |
| ETF flows | U.S. spot Bitcoin ETF inflows, outflows, and rolling totals | Farside Investors |

The warehouse defaults to `~/data/market.duckdb`. It is populated separately by
[data_stores](https://github.com/mikeoc61/data_stores); this dashboard reads it
and does not run an ingester. Use `--db /path/to/market.duckdb` to select another
file. See [host setup](docs/usage.md#where-it-runs) for node requirements.

## Browser dashboard

With the virtual environment active:

```bash
python -m pip install -e '.[web]'
btc-dashboard-web
```

Open [localhost:8001](http://localhost:8001). The page updates its readings in
place, keeps your question intact during updates, and includes **copy PNG** and
**save PNG** buttons. Images contain the page header, the readings and the
footer, leaving out the question and answer box.

For a dashboard running on another machine, use an SSH tunnel (replace
`your-node-host` with its SSH hostname):

```bash
ssh -L 8001:localhost:8001 your-node-host
```

Then open the same localhost address. Keep the default loopback binding: the
web server uses its own machine's provider credentials when answering questions.

For a persistent service, see the [systemd deployment guide](deploy/README.md).
After updating an editable installation, restart the web service so it loads
the new code.

## Everyday commands

```bash
btc-dashboard --refresh                      # collect again, bypassing disk cache
btc-dashboard --json > snapshot.json          # save the underlying data
btc-dashboard --html > dashboard.html         # save a self-contained page
btc-dashboard --from snapshot.json            # view a saved snapshot
btc-dashboard --context                      # inspect the analyst's facts, no LLM call
btc-dashboard --help
```

`--from` also accepts an HTTP(S) URL or `-` for stdin. This lets you collect data
on one machine and read it on another.

See [all CLI options](docs/usage.md#usage) for filtering, timeouts, colors, and
exit codes.

## Ask a question

Configure a provider key in your environment or the
[configuration file](docs/usage.md#the-env-file), then:

```bash
btc-dashboard --ask "What stands out in these readings?"
btc-dashboard --ask "How does this drawdown compare with earlier ones?"
btc-dashboard --ask "Summarize the current readings" --no-tools
```

Supported providers are **Anthropic** (the default), **OpenAI**, **DeepSeek**,
**OpenRouter**, and **Ollama**. OpenAI, OpenRouter, and Ollama require an explicit
model selection. See [provider configuration](docs/reference.md#choosing-a-provider).

When a local warehouse is available, the analyst can run read-only SQL to
answer historical questions. Queries are shown with the answer. `--no-tools`
limits it to the snapshot, and the dashboard tells you when historical queries
are unavailable.

**Where analysis runs:** CLI questions use the machine running the command;
browser questions use the machine running `btc-dashboard-web`. Importing a
remote snapshot does not grant access to the remote warehouse. Normal
collection and rendering make no LLM calls.

## Reading the dashboard

- **Dates and windows matter.** Live spot prices, daily warehouse closes, and
  ETF trading sessions describe different periods. Compare their stated dates.
- **`n/a` means a reading is unavailable**, rather than zero.
- **`cached` means a saved reading is within its cache lifetime.** Warehouse
  and flow data use a one-hour disk cache; price and node data do not. The web
  view also shares a snapshot for up to two minutes between collections.
- **`STALE` means a refresh failed and an older cached copy is being shown.**
  Copies older than four days are refused. Content can also lag independently
  of cache age, so check the source's dates and warnings.
- **NOTABLE highlights readings that cross defined thresholds.** These are
  observations, not forecasts.
- **Balance of evidence puts six readings side by side.** It does not combine
  them into a trading score.

For definitions, calculation windows, and caveats, read the
[measurement notes](docs/reference.md#measurement-notes).

## How it works

Four independent source collectors produce one JSON snapshot. Terminal, HTML,
JSON, and analyst views all use that snapshot. Historical SQL queries during
analysis are the explicit exception.

```text
Price APIs ──────────────┐
Bitcoin Core ────────────┤                   ┌─ Terminal / JSON / HTML
DuckDB warehouse ─ cache ├─ Shared snapshot ─┼─ Local web dashboard
Farside flows ──── cache ┘                   └─ Optional LLM analyst
```

Collection runs concurrently and handles source failures independently. Each
source owns its measurement definitions and presentation notes, keeping the
qualifiers close to the calculations.

Explore the [architecture](docs/reference.md#design),
[source interface](docs/reference.md#adding-a-source),
[related repositories](docs/reference.md#related-repositories), and
[historical studies](docs/reference.md#studies).

## Development

```bash
python -m pip install -e '.[dev]'
python -m pytest
```

Tests cover collection, real temporary DuckDB databases, cache behavior,
rendering, analyst tools, and web routes. Progress tests currently assume a
capable terminal: if your runner sets `TERM=dumb`, use `TERM=xterm python -m
pytest`. One web test needs permission to bind a local socket.

## License and data

[MIT](LICENSE) © 2026 Michael OConnor.

ETF flows come from [Farside Investors](https://farside.co.uk/); price data comes
from public APIs. Data is provided as is, without guarantees of accuracy,
completeness, or timeliness. This is informational tooling, not investment advice.
