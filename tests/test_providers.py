"""Provider selection and transport for --ask."""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from btc_dashboard import providers
from btc_dashboard.config import Config


class TestResolve:
    def test_bare_model_uses_the_default_provider(self):
        p, m = providers.resolve("claude-opus-5")
        assert (p.name, m) == ("anthropic", "claude-opus-5")

    def test_prefix_names_the_provider(self):
        p, m = providers.resolve("deepseek/deepseek-chat")
        assert (p.name, m) == ("deepseek", "deepseek-chat")

    def test_provider_argument_is_honoured(self):
        p, m = providers.resolve("llama3", "ollama")
        assert (p.name, m) == ("ollama", "llama3")

    def test_prefix_beats_the_provider_argument(self):
        """One env var can then carry both without a second setting."""
        p, m = providers.resolve("anthropic/claude-opus-5", "deepseek")
        assert (p.name, m) == ("anthropic", "claude-opus-5")

    def test_a_slash_inside_a_model_id_survives(self):
        """OpenRouter ids contain a slash; only a known provider prefix splits."""
        p, m = providers.resolve("meta/llama-3.1-70b", "openrouter")
        assert (p.name, m) == ("openrouter", "meta/llama-3.1-70b")

    def test_provider_with_no_model_uses_its_default(self):
        p, m = providers.resolve(None, "deepseek")
        assert m == "deepseek-chat"

    def test_provider_without_a_default_demands_one(self):
        with pytest.raises(providers.ProviderError, match="no default model"):
            providers.resolve(None, "openai")

    def test_unknown_provider_lists_the_options(self):
        with pytest.raises(providers.ProviderError, match="unknown provider") as e:
            providers.resolve("x", "bogus")
        assert "anthropic" in str(e.value)


class TestApiKey:
    def test_environment_wins(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
        assert providers.api_key(providers.PROVIDERS["deepseek"]) == "from-env"

    def test_falls_back_to_the_env_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        f = tmp_path / "env"
        f.write_text("OPENAI_API_KEY=other\nDEEPSEEK_API_KEY=from-file\n")
        monkeypatch.setenv("BTC_DASHBOARD_ENV", str(f))
        assert providers.api_key(providers.PROVIDERS["deepseek"]) == "from-file"

    def test_each_provider_reads_its_own_key(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        f = tmp_path / "env"
        f.write_text("OPENAI_API_KEY=oa\nDEEPSEEK_API_KEY=ds\n")
        monkeypatch.setenv("BTC_DASHBOARD_ENV", str(f))
        assert providers.api_key(providers.PROVIDERS["openai"]) == "oa"
        assert providers.api_key(providers.PROVIDERS["deepseek"]) == "ds"

    def test_a_local_provider_needs_none(self):
        assert providers.api_key(providers.PROVIDERS["ollama"]) is None

    def test_missing_key_is_none_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("BTC_DASHBOARD_ENV", str(tmp_path / "absent"))
        assert providers.api_key(providers.PROVIDERS["deepseek"]) is None


def _fake_http(monkeypatch, payload=None, error=None, capture=None):
    def urlopen(req, timeout=None):
        if capture is not None:
            capture["url"] = req.full_url
            capture["headers"] = dict(req.header_items())
            capture["body"] = json.loads(req.data.decode())
        if error is not None:
            raise error
        class R:
            def read(self): return json.dumps(payload).encode()
            def __enter__(self): return self
            def __exit__(self, *a): return False
        return R()
    monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)


def _ok(text="an answer"):
    return {
        "model": "deepseek-chat",
        "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22},
    }


class TestOpenAICompatibleTransport:
    def test_sends_system_and_user_messages(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        _fake_http(monkeypatch, payload=_ok(), capture=cap)
        providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat",
                           "SYS", "PROMPT")
        assert cap["url"].endswith("/chat/completions")
        roles = [m["role"] for m in cap["body"]["messages"]]
        assert roles == ["system", "user"]
        assert cap["body"]["messages"][0]["content"] == "SYS"

    def test_bearer_token_is_sent(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "secret")
        cap = {}
        _fake_http(monkeypatch, payload=_ok(), capture=cap)
        providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p")
        assert cap["headers"]["Authorization"] == "Bearer secret"

    def test_local_provider_sends_no_authorization(self, monkeypatch):
        cap = {}
        _fake_http(monkeypatch, payload=_ok(), capture=cap)
        providers.complete(providers.PROVIDERS["ollama"], "llama3", "s", "p")
        assert "Authorization" not in cap["headers"]

    def test_max_tokens_omitted_where_unsupported(self, monkeypatch):
        """OpenAI's newer models reject max_tokens; guessing would 400."""
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        cap = {}
        _fake_http(monkeypatch, payload=_ok(), capture=cap)
        providers.complete(providers.PROVIDERS["openai"], "some-model", "s", "p")
        assert "max_tokens" not in cap["body"]

    def test_effort_is_never_sent_to_openai_shaped_apis(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        _fake_http(monkeypatch, payload=_ok(), capture=cap)
        providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat",
                           "s", "p", effort="high")
        assert "output_config" not in cap["body"] and "effort" not in cap["body"]

    def test_returns_text_and_usage(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        _fake_http(monkeypatch, payload=_ok("hello"))
        done = providers.complete(providers.PROVIDERS["deepseek"],
                                  "deepseek-chat", "s", "p")
        assert done.text == "hello"
        assert (done.input_tokens, done.output_tokens) == (11, 22)

    def test_falls_back_to_reasoning_content(self, monkeypatch):
        """A reasoning model can spend its budget and return empty content."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        payload = _ok()
        payload["choices"][0]["message"] = {
            "content": "", "reasoning_content": "the reasoning"
        }
        _fake_http(monkeypatch, payload=payload)
        done = providers.complete(providers.PROVIDERS["deepseek"],
                                  "deepseek-chat", "s", "p")
        assert done.text == "the reasoning"

    def test_missing_key_is_reported_before_any_request(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        monkeypatch.setenv("BTC_DASHBOARD_ENV", str(tmp_path / "absent"))
        with pytest.raises(providers.ProviderError, match="DEEPSEEK_API_KEY is not set"):
            providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p")

    @pytest.mark.parametrize("code,expected", [
        (401, "rejected"), (404, "does not know that model"), (429, "rate limited"),
        (500, "API error 500"),
    ])
    def test_http_errors_are_translated(self, monkeypatch, code, expected):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        err = urllib.error.HTTPError(
            "u", code, "msg", {}, io.BytesIO(b'{"error":{"message":"detail"}}')
        )
        _fake_http(monkeypatch, error=err)
        with pytest.raises(providers.ProviderError, match=expected):
            providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p")

    def test_unreachable_host_names_the_endpoint(self, monkeypatch):
        _fake_http(monkeypatch, error=urllib.error.URLError("refused"))
        with pytest.raises(providers.ProviderError, match="could not reach ollama"):
            providers.complete(providers.PROVIDERS["ollama"], "llama3", "s", "p")

    def test_unexpected_shape_is_reported_clearly(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        _fake_http(monkeypatch, payload={"nonsense": True})
        with pytest.raises(providers.ProviderError, match="unexpected response shape"):
            providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p")


class TestAnalystIntegration:
    def _snap(self):
        return {"schema_version": 1, "generated_at": "2026-07-30T00:00:00+00:00",
                "asset": "btc", "sources": {}}

    def test_provider_is_reported_back(self, monkeypatch):
        from btc_dashboard import analyst
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        _fake_http(monkeypatch, payload=_ok("answer"))
        cfg = Config.from_env(provider="deepseek", model="deepseek-chat")
        r = analyst.ask(self._snap(), "q?", cfg)
        assert r.ok and r.provider == "deepseek" and r.text == "answer"

    def test_configuration_errors_surface_as_analyst_errors(self):
        from btc_dashboard import analyst
        cfg = Config.from_env(provider="bogus")
        r = analyst.ask(self._snap(), "q?", cfg)
        assert not r.ok and "unknown provider" in r.error

    def test_default_config_is_anthropic(self):
        cfg = Config.from_env()
        p, m = providers.resolve(cfg.model, cfg.provider)
        assert p.name == "anthropic" and m == "claude-opus-5"


class TestDefaultModelBelongsToTheProvider:
    """Regression: a config-level default model was applied to every provider.

    With DEFAULT_MODEL = "claude-opus-5" in config, `--provider openai` and no
    --model requested an Anthropic model id from OpenAI. The missing-key error
    masked it; with a key it would have sent a nonsense request.
    """

    def test_no_model_uses_the_chosen_providers_default(self):
        cfg = Config.from_env(provider="deepseek")
        p, m = providers.resolve(cfg.model, cfg.provider)
        assert (p.name, m) == ("deepseek", "deepseek-chat")

    def test_provider_without_a_default_errors_rather_than_borrowing_one(self):
        cfg = Config.from_env(provider="openai")
        with pytest.raises(providers.ProviderError, match="no default model"):
            providers.resolve(cfg.model, cfg.provider)

    def test_the_anthropic_default_still_applies_by_default(self):
        cfg = Config.from_env()
        p, m = providers.resolve(cfg.model, cfg.provider)
        assert (p.name, m) == ("anthropic", "claude-opus-5")

    def test_an_explicit_model_is_never_overridden(self):
        cfg = Config.from_env(provider="ollama", model="llama3")
        p, m = providers.resolve(cfg.model, cfg.provider)
        assert (p.name, m) == ("ollama", "llama3")
