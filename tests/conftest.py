"""Test isolation from the developer's real environment.

Without this, tests that build a snapshot read and write the operator's actual
cache directory: results would depend on whether the CLI had been run recently,
and the suite would overwrite real cached data. Both fixtures are autouse
because the failure mode is silent — a test passes locally and fails on a
machine where a cache happens to exist.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# tools/ holds runnable studies rather than package modules, so it is not on
# the import path by default. Adding it here lets them be unit-tested without
# turning every script into a package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))


@pytest.fixture(autouse=True)
def restore_environment():
    """Undo direct writes to os.environ made by the code under test.

    `config.load_env_file` sets variables with `os.environ.setdefault`, which
    is right in production — one process, loaded once — but monkeypatch only
    reverts what monkeypatch itself changed, so without this one test's env
    file leaks into the next and the failure looks like a bug in the code.
    """
    saved = os.environ.copy()
    yield
    os.environ.clear()
    os.environ.update(saved)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path_factory, monkeypatch):
    """Point every test at its own empty cache directory."""
    path = tmp_path_factory.mktemp("btc_cache")
    monkeypatch.setenv("BTC_DASHBOARD_CACHE", str(path))
    return path


@pytest.fixture(autouse=True)
def no_api_credentials(monkeypatch):
    """Make an accidental live API call impossible.

    The analyst reads a key from the environment or an env file; a test that
    reached the network would be slow, flaky, and would spend real money.
    """
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("BTC_DASHBOARD_ENV", "/nonexistent/btc_dashboard_env")
