"""Runtime configuration, resolved from the environment.

Everything the tool needs to locate its inputs lives here, so no other module
reads `os.environ` directly. That matters for the JSON-service phase: a server
process builds one Config at startup and passes it down, rather than each
source rediscovering its own paths per request.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_DB_PATH = Path.home() / "data" / "market.duckdb"
APP_NAME = "btc_dashboard"
DEFAULT_PROVIDER = "anthropic"
# Deliberately no default model here: it belongs to the provider, not to the
# config. A default of "claude-opus-5" would be silently applied to whatever
# provider was selected, so `--provider openai` would request an Anthropic
# model id from OpenAI. Unset means "use the chosen provider's own default".
DEFAULT_MODEL = None
DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT = 20
# Per-source default; a source opts in by declaring CACHE_TTL.
DEFAULT_CACHE_TTL = 3600


def default_cache_dir() -> Path:
    """`$XDG_CACHE_HOME/btc_dashboard`, else `~/.cache/btc_dashboard`.

    Resolved on each call rather than at import so the environment can be
    changed after the module loads — which is how the test suite isolates
    itself from the operator's real cache.

    No fallback to the pre-XDG location, unlike the config path below: cache
    contents are disposable, so an install that moves simply refetches once.
    Honouring an old directory when it happened to exist would mean the move
    never actually took effect for the people who already had one.
    """
    base = os.environ.get("XDG_CACHE_HOME")
    return (Path(base) if base else Path.home() / ".cache") / APP_NAME


def default_config_dir() -> Path:
    """`$XDG_CONFIG_HOME/btc_dashboard`, else `~/.config/btc_dashboard`."""
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / APP_NAME


def env_file_candidates(explicit: str | Path | None = None) -> list[Path]:
    """Where the env file may live, in order.

    `BTC_DASHBOARD_ENV` wins outright. Otherwise the XDG config location is
    preferred, with the pre-XDG path still read so an install predating the
    move keeps working — that file may hold an API key, and silently ceasing
    to find it is a bad way to learn about a path change.
    """
    if explicit:
        return [Path(explicit)]
    override = os.environ.get("BTC_DASHBOARD_ENV")
    if override:
        return [Path(override)]
    return [default_config_dir() / "env", Path.home() / ".btc_dashboard" / "env"]


def load_env_file(explicit: str | Path | None = None) -> dict[str, str]:
    """Read `KEY=value` lines from the env file into the process environment.

    The file is named `env` and holds `KEY=value` lines, so it should behave
    like one: it sets *any* variable, not only API keys. It previously fed
    only the credential lookup, which meant a `BTC_DASHBOARD_MODEL=` line in
    it was silently ignored while the key beside it worked.

    A variable already present in the real environment is left alone, so an
    explicit `export` or a one-off `VAR=x btc-dashboard` still wins over the
    file. Parsing is deliberately dumb — split on the first `=`, strip one
    layer of quotes, skip blanks, comments and anything malformed. Nothing is
    executed, so the file cannot do more than set variables.
    """
    loaded: dict[str, str] = {}
    for path in env_file_candidates(explicit):
        try:
            text = path.read_text()
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, sep, value = line.partition("=")
            key = key.strip()
            if not sep or not key:
                continue
            # Setting this from inside the file it selects would be circular.
            if key == "BTC_DASHBOARD_ENV":
                continue
            value = value.strip().strip("'\"")
            loaded[key] = value
            os.environ.setdefault(key, value)
        # First readable file wins; the second path is a fallback, not a merge.
        break
    return loaded


@dataclass(frozen=True)
class Config:
    """Resolved paths and settings for one invocation."""

    db_path: Path
    bitcoin_cli: str
    cache_dir: Path
    timeout: int
    provider: str
    model: str | None
    effort: str
    cache_ttl: int

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        # Fold the env file in first so its settings are visible below. Real
        # environment variables are left untouched, so this only fills gaps.
        load_env_file()
        # The warehouse file is shared with the ingester that writes it, so its
        # own env var is honoured as a fallback — a host that has already set
        # MARKET_WAREHOUSE_DB shouldn't need to set a second variable naming
        # the same file. This tool only ever opens it read-only.
        db = (
            os.environ.get("BTC_DASHBOARD_DB")
            or os.environ.get("MARKET_WAREHOUSE_DB")
        )
        base = cls(
            db_path=Path(db) if db else DEFAULT_DB_PATH,
            bitcoin_cli=os.environ.get("BTC_DASHBOARD_BITCOIN_CLI", "bitcoin-cli"),
            cache_dir=Path(
                os.environ.get("BTC_DASHBOARD_CACHE") or default_cache_dir()
            ),
            timeout=int(os.environ.get("BTC_DASHBOARD_TIMEOUT", DEFAULT_TIMEOUT)),
            provider=os.environ.get("BTC_DASHBOARD_PROVIDER", DEFAULT_PROVIDER),
            model=os.environ.get("BTC_DASHBOARD_MODEL") or DEFAULT_MODEL,
            effort=os.environ.get("BTC_DASHBOARD_EFFORT", DEFAULT_EFFORT),
            cache_ttl=int(
                os.environ.get("BTC_DASHBOARD_CACHE_TTL", DEFAULT_CACHE_TTL)
            ),
        )
        return base.replace(**overrides) if overrides else base

    def replace(self, **kw) -> "Config":
        from dataclasses import replace as _replace

        return _replace(self, **{k: v for k, v in kw.items() if v is not None})
