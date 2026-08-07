"""TTL cache for the data plane.

The cache has to get three things right, and they pull against each other:
serve fresh data without refetching, survive a failed refresh by serving the
expired copy *labelled*, and never let a bad cache file become an error.
"""
from __future__ import annotations

import datetime
import json
import types

import pytest

from btc_dashboard import cache, render, snapshot
from btc_dashboard.config import Config
from btc_dashboard.sources import SourceResult


def _cfg(tmp_path, **kw):
    return Config.from_env(cache_dir=tmp_path, **kw)


def _module(name="demo", ttl=3600, results=None):
    """A fake source that records how many times it was collected."""
    mod = types.SimpleNamespace(NAME=name, CACHE_TTL=ttl, calls=0)
    queue = list(results or [])

    def collect(cfg):
        mod.calls += 1
        if queue:
            return queue.pop(0)
        return SourceResult(name, True, data={"n": mod.calls})

    mod.collect = collect
    return mod


def _age_cache(path, seconds):
    """Backdate a cache file so TTL behaviour can be tested without sleeping."""
    payload = json.loads(path.read_text())
    stamp = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(
        seconds=seconds
    )
    payload[cache.CACHED_AT] = stamp.isoformat()
    path.write_text(json.dumps(payload))


class TestTTL:
    def test_second_call_within_ttl_does_not_recollect(self, tmp_path):
        mod, cfg = _module(), _cfg(tmp_path)
        first = cache.collect(mod, cfg)
        second = cache.collect(mod, cfg)

        assert mod.calls == 1, "a within-TTL hit must not re-collect"
        assert second.data == first.data
        assert second.cached is True and second.stale is False
        assert second.cache_ttl_seconds == 3600

    def test_expired_cache_recollects(self, tmp_path):
        mod, cfg = _module(), _cfg(tmp_path)
        cache.collect(mod, cfg)
        _age_cache(cache.path_for(cfg, mod.NAME), 3601)

        result = cache.collect(mod, cfg)
        assert mod.calls == 2
        assert result.cached is False
        assert result.data == {"n": 2}

    def test_refresh_bypasses_a_fresh_cache(self, tmp_path):
        mod, cfg = _module(), _cfg(tmp_path)
        cache.collect(mod, cfg)
        result = cache.collect(mod, cfg, refresh=True)

        assert mod.calls == 2
        assert result.cached is False
        # and the refreshed value replaced the cached one
        assert cache.collect(mod, cfg).data == {"n": 2}

    def test_ttl_zero_disables_the_read_path(self, tmp_path):
        mod, cfg = _module(), _cfg(tmp_path, cache_ttl=0)
        cache.collect(mod, cfg)
        cache.collect(mod, cfg)
        assert mod.calls == 2

    def test_source_without_ttl_is_never_cached(self, tmp_path):
        mod, cfg = _module(ttl=None), _cfg(tmp_path)
        cache.collect(mod, cfg)
        cache.collect(mod, cfg)
        assert mod.calls == 2
        assert not cache.path_for(cfg, mod.NAME).exists()

    def test_cache_age_is_reported(self, tmp_path):
        mod, cfg = _module(), _cfg(tmp_path)
        cache.collect(mod, cfg)
        _age_cache(cache.path_for(cfg, mod.NAME), 900)

        result = cache.collect(mod, cfg)
        assert result.cached is True
        assert 890 <= result.cache_age_seconds <= 910


class TestFailureFallback:
    def test_expired_cache_rescues_a_failed_refresh(self, tmp_path):
        good = SourceResult("demo", True, data={"v": "good"})
        bad = SourceResult("demo", False, error="site unreachable")
        mod, cfg = _module(results=[good, bad]), _cfg(tmp_path)

        cache.collect(mod, cfg)
        _age_cache(cache.path_for(cfg, mod.NAME), 7200)
        result = cache.collect(mod, cfg)

        assert result.available and result.data == {"v": "good"}
        assert result.stale is True and result.cached is True
        # The live failure's reason is carried, not replaced by silence.
        assert "site unreachable" in result.error
        assert result.cache_age_seconds >= 7000

    def test_failure_with_no_cache_stays_unavailable(self, tmp_path):
        bad = SourceResult("demo", False, error="site unreachable")
        mod, cfg = _module(results=[bad]), _cfg(tmp_path)

        result = cache.collect(mod, cfg)
        assert result.available is False
        assert result.error == "site unreachable"

    def test_an_unavailable_result_is_not_cached(self, tmp_path):
        bad = SourceResult("demo", False, error="nope")
        mod, cfg = _module(results=[bad]), _cfg(tmp_path)
        cache.collect(mod, cfg)
        assert not cache.path_for(cfg, mod.NAME).exists()


class TestCorruptCache:
    @pytest.mark.parametrize("content", [
        "", "not json", "[]", '{"no_timestamp": 1}',
        '{"cached_at": "gibberish", "source": {}}',
        '{"cached_at": "2026-01-01T00:00:00+00:00"}',
    ])
    def test_unusable_cache_is_a_miss_not_an_error(self, tmp_path, content):
        mod, cfg = _module(), _cfg(tmp_path)
        path = cache.path_for(cfg, mod.NAME)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

        result = cache.collect(mod, cfg)
        assert result.available and mod.calls == 1

    def test_a_future_timestamp_is_treated_as_expired(self, tmp_path):
        """A clock correction must not pin a cache as fresh indefinitely."""
        mod, cfg = _module(), _cfg(tmp_path)
        cache.collect(mod, cfg)
        _age_cache(cache.path_for(cfg, mod.NAME), -86400)

        cache.collect(mod, cfg)
        assert mod.calls == 2

    def test_missing_directory_is_created(self, tmp_path):
        mod = _module()
        cfg = _cfg(tmp_path / "deep" / "nested")
        assert cache.collect(mod, cfg).available
        assert cache.path_for(cfg, mod.NAME).exists()

    def test_write_failure_does_not_lose_the_result(self, tmp_path, monkeypatch):
        mod, cfg = _module(), _cfg(tmp_path)
        monkeypatch.setattr(cache, "write", lambda *a, **k: False)
        assert cache.collect(mod, cfg).available is True


class TestWhichSourcesCache:
    def test_onchain_and_flows_cache_for_an_hour(self):
        from btc_dashboard.sources import flows, warehouse
        assert warehouse.CACHE_TTL == 3600
        assert flows.CACHE_TTL == 3600

    def test_live_sources_are_not_cached(self):
        """Serving a 40-minute-old mempool or spot price as current would be
        worse than not showing it."""
        from btc_dashboard.sources import node, price
        assert getattr(price, "CACHE_TTL", None) is None
        assert getattr(node, "CACHE_TTL", None) is None


class TestPresentation:
    def _block(self, **kw):
        base = {
            "available": True, "stale": False, "cached": False,
            "cache_age_seconds": None, "cache_ttl_seconds": None,
            "as_of": None, "error": None,
            "data": {"spot": 1.0, "source": "x", "sma200": None,
                     "sma200_pct": None, "sma200_position": None,
                     "days_available": 5},
        }
        base.update(kw)
        return {"schema_version": 1, "generated_at": "2026-07-30T00:00:00+00:00",
                "asset": "btc", "sources": {"price": base}}

    @pytest.mark.parametrize("seconds,expected", [
        (45, "45s"), (900, "15m"), (7200, "2h"), (172800, "2d"), (None, "?"),
    ])
    def test_human_age(self, seconds, expected):
        assert render.human_age(seconds) == expected

    def test_cached_and_stale_render_differently(self):
        cached = render.render(self._block(cached=True, cache_age_seconds=900))
        stale = render.render(
            self._block(cached=True, stale=True, cache_age_seconds=7200,
                        error="site unreachable")
        )
        assert "[cached 15m]" in cached and "STALE" not in cached
        assert "[STALE 2h]" in stale
        assert "live refresh failed: site unreachable" in stale

    def test_uncached_block_has_no_marker(self):
        out = render.render(self._block())
        assert "[cached" not in out and "[STALE" not in out

    def test_analyst_is_told_the_data_is_cached(self):
        from btc_dashboard import analyst
        ctx = analyst.build_context(self._block(cached=True, cache_age_seconds=900))
        assert "collected 15m ago" in ctx
        assert "WARNING" not in ctx

    def test_analyst_is_warned_about_stale_data(self):
        from btc_dashboard import analyst
        ctx = analyst.build_context(
            self._block(cached=True, stale=True, cache_age_seconds=7200)
        )
        assert "WARNING" in ctx and "2h" in ctx


class TestSnapshotIntegration:
    def test_build_serves_cached_sources_on_the_second_call(self, tmp_path, monkeypatch):
        from btc_dashboard.sources import flows, node, price, warehouse

        calls = {m.NAME: 0 for m in snapshot.SOURCES}

        def stub(mod):
            def collect(cfg):
                calls[mod.NAME] += 1
                return SourceResult(mod.NAME, True, data={"n": calls[mod.NAME]})
            return collect

        for mod in (price, node, warehouse, flows):
            monkeypatch.setattr(mod, "collect", stub(mod))

        cfg = _cfg(tmp_path)
        snapshot.build(cfg)
        snapshot.build(cfg)

        # Cached sources collected once; live sources every time.
        assert calls["warehouse"] == 1 and calls["flows"] == 1
        assert calls["price"] == 2 and calls["node"] == 2

    def test_refresh_recollects_everything(self, tmp_path, monkeypatch):
        from btc_dashboard.sources import flows, warehouse

        calls = {"warehouse": 0, "flows": 0}
        for mod in (warehouse, flows):
            def collect(cfg, m=mod):
                calls[m.NAME] += 1
                return SourceResult(m.NAME, True, data={})
            monkeypatch.setattr(mod, "collect", collect)

        cfg = _cfg(tmp_path)
        snapshot.build(cfg, only=("warehouse", "flows"))
        snapshot.build(cfg, only=("warehouse", "flows"), refresh=True)
        assert calls == {"warehouse": 2, "flows": 2}


class TestTimeDerivedFieldsAreRecomputed:
    """Fields relative to *now* must not be served frozen from cache.

    Regression: moving caching out of flows.collect() dropped its age_days
    re-derivation, so a three-day-old stale payload would still claim the
    trading day was "1d ago" — exactly when the reader most needs the truth.
    """

    def test_flows_age_is_recomputed_on_a_cache_read(self, tmp_path, monkeypatch):
        import datetime as dt
        from btc_dashboard.sources import flows

        # Cache a payload whose stored age_days is deliberately wrong.
        stored = SourceResult(
            "flows", True,
            data={"as_of": "28 Jul 2026", "age_days": 1, "lead": "IBIT"},
            as_of="28 Jul 2026",
        )
        mod = _module(name="flows", results=[stored])
        mod.refresh_derived = flows.refresh_derived
        cfg = _cfg(tmp_path)
        cache.collect(mod, cfg)

        # Read it back three days later.
        monkeypatch.setattr(flows, "_market_today", lambda: dt.date(2026, 7, 31))
        result = cache.collect(mod, cfg)

        assert result.cached is True
        assert result.data["age_days"] == 3, "stale cache must not claim to be fresh"

    def test_warehouse_days_behind_is_recomputed(self, tmp_path):
        import datetime as dt
        from btc_dashboard.sources import warehouse

        old = (dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=9))
        stored = SourceResult(
            "warehouse", True,
            data={"date": old.isoformat(), "days_behind": 0,
                  "warehouse_stale": False, "onchain": {}, "signals": {}},
        )
        mod = _module(name="warehouse", results=[stored])
        mod.refresh_derived = warehouse.refresh_derived
        cfg = _cfg(tmp_path)
        cache.collect(mod, cfg)

        result = cache.collect(mod, cfg)
        assert result.data["days_behind"] == 9
        assert result.data["warehouse_stale"] is True

    def test_rederive_also_runs_on_the_stale_fallback_path(self, tmp_path, monkeypatch):
        import datetime as dt
        from btc_dashboard.sources import flows

        good = SourceResult(
            "flows", True,
            data={"as_of": "28 Jul 2026", "age_days": 1}, as_of="28 Jul 2026",
        )
        bad = SourceResult("flows", False, error="unreachable")
        mod = _module(name="flows", results=[good, bad])
        mod.refresh_derived = flows.refresh_derived
        cfg = _cfg(tmp_path)

        cache.collect(mod, cfg)
        _age_cache(cache.path_for(cfg, "flows"), 86400 * 3)
        monkeypatch.setattr(flows, "_market_today", lambda: dt.date(2026, 7, 31))

        result = cache.collect(mod, cfg)
        assert result.stale is True
        assert result.data["age_days"] == 3

    def test_a_broken_hook_does_not_lose_the_data(self, tmp_path):
        def boom(data):
            raise RuntimeError("hook is broken")

        mod = _module()
        mod.refresh_derived = boom
        cfg = _cfg(tmp_path)
        cache.collect(mod, cfg)
        result = cache.collect(mod, cfg)
        assert result.available and result.cached is True

    def test_sources_with_time_relative_fields_declare_the_hook(self):
        from btc_dashboard.sources import flows, warehouse
        assert callable(flows.refresh_derived)
        assert callable(warehouse.refresh_derived)


class TestCachePathFollowsXDG:
    """Cache belongs under $XDG_CACHE_HOME/btc_dashboard, not a dotdir in $HOME."""

    def test_defaults_to_dot_cache(self, monkeypatch, tmp_path):
        from btc_dashboard import config
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
        assert config.default_cache_dir() == tmp_path / ".cache" / "btc_dashboard"

    def test_honours_xdg_cache_home(self, monkeypatch, tmp_path):
        from btc_dashboard import config
        monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
        assert config.default_cache_dir() == tmp_path / "xdg" / "btc_dashboard"

    def test_config_defaults_to_dot_config(self, monkeypatch, tmp_path):
        from btc_dashboard import config
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))
        assert config.default_config_dir() == tmp_path / ".config" / "btc_dashboard"

    def test_honours_xdg_config_home(self, monkeypatch, tmp_path):
        from btc_dashboard import config
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        assert config.default_config_dir() == tmp_path / "xdg" / "btc_dashboard"

    def test_explicit_env_var_still_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BTC_DASHBOARD_CACHE", str(tmp_path / "custom"))
        assert Config.from_env().cache_dir == tmp_path / "custom"


class TestEnvFileLookup:
    """The key file is NOT disposable, so the pre-XDG path stays readable.

    Lives with the providers now: each provider reads its own key, so the
    lookup could no longer be Anthropic-specific.
    """

    def test_prefers_xdg_config_but_falls_back(self, monkeypatch, tmp_path):
        from btc_dashboard import config
        monkeypatch.delenv("BTC_DASHBOARD_ENV", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(config.Path, "home", staticmethod(lambda: tmp_path))

        paths = config.env_file_candidates()
        assert paths[0] == tmp_path / "cfg" / "btc_dashboard" / "env"
        assert paths[1] == tmp_path / ".btc_dashboard" / "env"

    def test_explicit_path_wins_outright(self, monkeypatch, tmp_path):
        from btc_dashboard import config
        monkeypatch.setenv("BTC_DASHBOARD_ENV", str(tmp_path / "only"))
        assert config.env_file_candidates() == [tmp_path / "only"]

    def test_key_is_read_from_the_legacy_location(self, monkeypatch, tmp_path):
        from btc_dashboard import providers
        monkeypatch.delenv("BTC_DASHBOARD_ENV", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(providers.config.Path, "home", staticmethod(lambda: tmp_path))

        legacy = tmp_path / ".btc_dashboard" / "env"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("ANTHROPIC_API_KEY=sk-ant-legacy\n")
        assert providers.api_key(providers.PROVIDERS["anthropic"]) == "sk-ant-legacy"

    def test_xdg_location_takes_precedence(self, monkeypatch, tmp_path):
        from btc_dashboard import providers
        monkeypatch.delenv("BTC_DASHBOARD_ENV", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
        monkeypatch.setattr(providers.config.Path, "home", staticmethod(lambda: tmp_path))

        for path, key in (
            (tmp_path / "cfg" / "btc_dashboard" / "env", "sk-ant-new"),
            (tmp_path / ".btc_dashboard" / "env", "sk-ant-legacy"),
        ):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"ANTHROPIC_API_KEY={key}\n")
        assert providers.api_key(providers.PROVIDERS["anthropic"]) == "sk-ant-new"
