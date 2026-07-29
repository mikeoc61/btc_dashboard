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
DEFAULT_CACHE_DIR = Path.home() / ".btc_dashboard" / "cache"
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "high"
DEFAULT_TIMEOUT = 20


@dataclass(frozen=True)
class Config:
    """Resolved paths and settings for one invocation."""

    db_path: Path
    bitcoin_cli: str
    cache_dir: Path
    timeout: int
    model: str
    effort: str

    @classmethod
    def from_env(cls, **overrides) -> "Config":
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
                os.environ.get("BTC_DASHBOARD_CACHE", str(DEFAULT_CACHE_DIR))
            ),
            timeout=int(os.environ.get("BTC_DASHBOARD_TIMEOUT", DEFAULT_TIMEOUT)),
            model=os.environ.get("BTC_DASHBOARD_MODEL", DEFAULT_MODEL),
            effort=os.environ.get("BTC_DASHBOARD_EFFORT", DEFAULT_EFFORT),
        )
        return base.replace(**overrides) if overrides else base

    def replace(self, **kw) -> "Config":
        from dataclasses import replace as _replace

        return _replace(self, **{k: v for k, v in kw.items() if v is not None})
