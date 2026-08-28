"""Provider selection and transport for --ask."""
from __future__ import annotations

import io
import json
import sys
import types
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
        """A provider that declares no max_tokens support must not be sent one.

        Exercised through a synthetic provider rather than `openai`, which now
        speaks Responses: the flag governs this chat-completions body, and the
        test should keep testing that branch rather than follow openai away
        from it.
        """
        provider = providers.Provider(
            name="capped", kind="openai", base_url="https://example.invalid/v1",
            env_key=None, default_model="m", supports_max_tokens=False)
        cap = {}
        _fake_http(monkeypatch, payload=_ok(), capture=cap)
        providers.complete(provider, "some-model", "s", "p")
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


class TestEnvFileSetsAnyVariable:
    """The file is named `env` and holds KEY=value lines, so it must behave
    like one.

    Regression: it was only scanned for API keys, so
    `BTC_DASHBOARD_MODEL=openai/...` in it was silently ignored while the
    OPENAI_API_KEY line beside it worked — the tool quietly fell back to the
    Anthropic default.
    """

    def _file(self, monkeypatch, tmp_path, text):
        f = tmp_path / "env"
        f.write_text(text)
        monkeypatch.setenv("BTC_DASHBOARD_ENV", str(f))
        return f

    def test_model_from_the_env_file_is_used(self, monkeypatch, tmp_path):
        monkeypatch.delenv("BTC_DASHBOARD_MODEL", raising=False)
        self._file(monkeypatch, tmp_path, "BTC_DASHBOARD_MODEL=openai/gpt-5.6-luna\n")
        cfg = Config.from_env()
        p, m = providers.resolve(cfg.model, cfg.provider)
        assert (p.name, m) == ("openai", "gpt-5.6-luna")

    def test_any_setting_is_read_not_just_keys(self, monkeypatch, tmp_path):
        for var in ("BTC_DASHBOARD_EFFORT", "BTC_DASHBOARD_CACHE_TTL",
                    "BTC_DASHBOARD_PROVIDER"):
            monkeypatch.delenv(var, raising=False)
        self._file(monkeypatch, tmp_path,
                   "BTC_DASHBOARD_EFFORT=low\nBTC_DASHBOARD_CACHE_TTL=60\n"
                   "BTC_DASHBOARD_PROVIDER=deepseek\n")
        cfg = Config.from_env()
        assert (cfg.effort, cfg.cache_ttl, cfg.provider) == ("low", 60, "deepseek")

    def test_the_real_environment_wins(self, monkeypatch, tmp_path):
        """An explicit export or `VAR=x btc-dashboard` must beat the file."""
        self._file(monkeypatch, tmp_path, "BTC_DASHBOARD_MODEL=from-file\n")
        monkeypatch.setenv("BTC_DASHBOARD_MODEL", "from-env")
        assert Config.from_env().model == "from-env"

    def test_keys_still_work_alongside_settings(self, monkeypatch, tmp_path):
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        self._file(monkeypatch, tmp_path,
                   "BTC_DASHBOARD_MODEL=deepseek/deepseek-chat\nDEEPSEEK_API_KEY=k\n")
        assert providers.api_key(providers.PROVIDERS["deepseek"]) == "k"
        assert Config.from_env().model == "deepseek/deepseek-chat"

    @pytest.mark.parametrize("line,key,value", [
        ("export BTC_DASHBOARD_EFFORT=low", "BTC_DASHBOARD_EFFORT", "low"),
        ("BTC_DASHBOARD_EFFORT='low'", "BTC_DASHBOARD_EFFORT", "low"),
        ('BTC_DASHBOARD_EFFORT="low"', "BTC_DASHBOARD_EFFORT", "low"),
        ("  BTC_DASHBOARD_EFFORT = low  ", "BTC_DASHBOARD_EFFORT", "low"),
    ])
    def test_tolerates_common_shell_spellings(self, monkeypatch, tmp_path,
                                              line, key, value):
        monkeypatch.delenv(key, raising=False)
        self._file(monkeypatch, tmp_path, line + "\n")
        from btc_dashboard import config
        assert config.load_env_file().get(key) == value

    @pytest.mark.parametrize("junk", ["", "   ", "# a comment", "no-equals-here"])
    def test_blanks_comments_and_junk_are_skipped(self, monkeypatch, tmp_path, junk):
        self._file(monkeypatch, tmp_path, junk + "\nBTC_DASHBOARD_EFFORT=low\n")
        monkeypatch.delenv("BTC_DASHBOARD_EFFORT", raising=False)
        from btc_dashboard import config
        assert config.load_env_file()["BTC_DASHBOARD_EFFORT"] == "low"

    def test_the_file_cannot_redirect_which_file_is_read(self, monkeypatch, tmp_path):
        """BTC_DASHBOARD_ENV selects the file, so setting it from inside would
        be circular."""
        self._file(monkeypatch, tmp_path, "BTC_DASHBOARD_ENV=/somewhere/else\n")
        from btc_dashboard import config
        assert "BTC_DASHBOARD_ENV" not in config.load_env_file()

    def test_a_missing_file_is_not_an_error(self, monkeypatch, tmp_path):
        monkeypatch.setenv("BTC_DASHBOARD_ENV", str(tmp_path / "absent"))
        from btc_dashboard import config
        assert config.load_env_file() == {}


# --- tool use -------------------------------------------------------------

def _block(**kw):
    return types.SimpleNamespace(**kw)


def _resp(*, content, stop_reason="end_turn", model="claude-x", tin=10, tout=20):
    return types.SimpleNamespace(
        content=list(content), stop_reason=stop_reason, stop_details=None,
        model=model, usage=types.SimpleNamespace(input_tokens=tin, output_tokens=tout),
    )


def _fake_anthropic(monkeypatch, responses, capture=None):
    """Install a fake `anthropic` module that replays `responses` in order."""
    sent = []

    class Messages:
        def create(self, **kwargs):
            sent.append(kwargs)
            if capture is not None:
                capture["sent"] = sent
            return responses[len(sent) - 1]

    class Anthropic:
        def __init__(self, **kw):
            self.messages = Messages()

    mod = types.ModuleType("anthropic")
    mod.Anthropic = Anthropic
    for name in ("AuthenticationError", "NotFoundError", "RateLimitError",
                 "APIStatusError", "APIConnectionError"):
        setattr(mod, name, type(name, (Exception,), {}))
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    return sent


def _tool(name="query_warehouse", run=None):
    from btc_dashboard.sources import Tool
    return Tool(name, "run sql", {"type": "object",
                                  "properties": {"sql": {"type": "string"}}},
                run or (lambda sql="": f"rows for {sql}"))


class TestAnthropicToolLoop:
    def test_a_tool_call_is_run_and_the_answer_returned(self, monkeypatch):
        sent = _fake_anthropic(monkeypatch, [
            _resp(stop_reason="tool_use", content=[
                _block(type="tool_use", id="t1", name="query_warehouse",
                       input={"sql": "SELECT 1"})]),
            _resp(content=[_block(type="text", text="the answer")]),
        ])
        ran = []
        done = providers.complete(
            providers.PROVIDERS["anthropic"], "claude-x", "SYS", "PROMPT",
            tools=(_tool(),),
            run_tool=lambda n, a: ran.append((n, a)) or "42 rows",
        )
        assert done.text == "the answer"
        assert ran == [("query_warehouse", {"sql": "SELECT 1"})]
        assert done.tool_calls[0].arguments == {"sql": "SELECT 1"}
        assert done.tool_calls[0].result == "42 rows"
        assert len(sent) == 2, "the loop should continue after a tool call"

    def test_the_result_is_sent_back_with_its_call_id(self, monkeypatch):
        sent = _fake_anthropic(monkeypatch, [
            _resp(stop_reason="tool_use", content=[
                _block(type="tool_use", id="abc", name="query_warehouse", input={})]),
            _resp(content=[_block(type="text", text="ok")]),
        ])
        providers.complete(providers.PROVIDERS["anthropic"], "claude-x", "s", "p",
                           tools=(_tool(),), run_tool=lambda n, a: "ROWS")
        results = sent[1]["messages"][-1]["content"]
        assert results[0]["tool_use_id"] == "abc" and results[0]["content"] == "ROWS"

    def test_the_whole_assistant_turn_is_echoed_back(self, monkeypatch):
        """With thinking on, the API requires the thinking blocks that preceded
        a tool call to come back with it. Sending only the tool_use block 400s."""
        thinking = _block(type="thinking", thinking="hmm")
        use = _block(type="tool_use", id="t", name="query_warehouse", input={})
        sent = _fake_anthropic(monkeypatch, [
            _resp(stop_reason="tool_use", content=[thinking, use]),
            _resp(content=[_block(type="text", text="ok")]),
        ])
        providers.complete(providers.PROVIDERS["anthropic"], "claude-x", "s", "p",
                           tools=(_tool(),), run_tool=lambda n, a: "r")
        echoed = sent[1]["messages"][1]["content"]
        assert thinking in echoed and use in echoed

    def test_tokens_accumulate_over_every_round(self, monkeypatch):
        """The operator paid for all of them. Reporting only the last round
        understates the cost of exactly the questions that cost most."""
        _fake_anthropic(monkeypatch, [
            _resp(stop_reason="tool_use", tin=100, tout=5, content=[
                _block(type="tool_use", id="t", name="query_warehouse", input={})]),
            _resp(tin=300, tout=50, content=[_block(type="text", text="ok")]),
        ])
        done = providers.complete(providers.PROVIDERS["anthropic"], "claude-x",
                                  "s", "p", tools=(_tool(),),
                                  run_tool=lambda n, a: "r")
        assert (done.input_tokens, done.output_tokens) == (400, 55)

    def test_the_last_round_withholds_the_tools(self, monkeypatch):
        """Otherwise a model that keeps querying ends the loop on a turn that
        carries no text at all, and there is nothing to show."""
        rounds = providers.MAX_TOOL_ROUNDS
        sent = _fake_anthropic(monkeypatch, [
            _resp(stop_reason="tool_use", content=[
                _block(type="tool_use", id=f"t{i}", name="query_warehouse", input={})])
            for i in range(rounds)
        ] + [_resp(content=[_block(type="text", text="forced answer")])])
        done = providers.complete(providers.PROVIDERS["anthropic"], "claude-x",
                                  "s", "p", tools=(_tool(),),
                                  run_tool=lambda n, a: "r")
        assert done.text == "forced answer"
        assert "tools" in sent[0] and "tools" not in sent[-1]
        assert len(done.tool_calls) == rounds

    def test_no_tools_sends_no_tools_field(self, monkeypatch):
        sent = _fake_anthropic(monkeypatch, [
            _resp(content=[_block(type="text", text="plain")])])
        done = providers.complete(providers.PROVIDERS["anthropic"], "claude-x",
                                  "s", "p")
        assert done.text == "plain" and "tools" not in sent[0]
        assert done.tool_calls == ()

    def test_a_refusal_is_still_reported(self, monkeypatch):
        _fake_anthropic(monkeypatch, [
            types.SimpleNamespace(
                content=[], stop_reason="refusal",
                stop_details=types.SimpleNamespace(category="x"),
                model="m", usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))])
        with pytest.raises(providers.ProviderError, match="declined"):
            providers.complete(providers.PROVIDERS["anthropic"], "claude-x", "s", "p")


class TestOpenAIToolLoop:
    def _call(self, cid="c1", name="query_warehouse", arguments='{"sql": "SELECT 1"}'):
        return {"id": cid, "type": "function",
                "function": {"name": name, "arguments": arguments}}

    def _payload(self, message, finish="stop"):
        return {"model": "deepseek-chat",
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20}}

    def _http(self, monkeypatch, payloads, capture):
        bodies = []

        def urlopen(req, timeout=None):
            bodies.append(json.loads(req.data.decode()))
            capture["bodies"] = bodies
            payload = payloads[len(bodies) - 1]

            class R:
                def read(self): return json.dumps(payload).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
        return bodies

    def test_a_tool_call_is_run_and_the_answer_returned(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        self._http(monkeypatch, [
            self._payload({"role": "assistant", "content": None,
                           "tool_calls": [self._call()]}, finish="tool_calls"),
            self._payload({"role": "assistant", "content": "the answer"}),
        ], cap)
        ran = []
        done = providers.complete(
            providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p",
            tools=(_tool(),), run_tool=lambda n, a: ran.append((n, a)) or "42 rows")
        assert done.text == "the answer"
        assert ran == [("query_warehouse", {"sql": "SELECT 1"})]
        assert done.tool_calls[0].result == "42 rows"

    def test_the_result_goes_back_as_a_tool_message(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        self._http(monkeypatch, [
            self._payload({"role": "assistant", "content": None,
                           "tool_calls": [self._call(cid="xyz")]}, finish="tool_calls"),
            self._payload({"role": "assistant", "content": "done"}),
        ], cap)
        providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p",
                           tools=(_tool(),), run_tool=lambda n, a: "ROWS")
        last = cap["bodies"][1]["messages"][-1]
        assert last["role"] == "tool" and last["tool_call_id"] == "xyz"
        assert last["content"] == "ROWS"

    def test_the_tool_is_declared_in_the_openai_shape(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        self._http(monkeypatch, [self._payload({"content": "hi"})], cap)
        providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p",
                           tools=(_tool(),), run_tool=lambda n, a: "")
        spec = cap["bodies"][0]["tools"][0]
        assert spec["type"] == "function"
        assert spec["function"]["name"] == "query_warehouse"
        assert spec["function"]["parameters"]["properties"]["sql"]["type"] == "string"

    def test_malformed_arguments_come_back_as_a_tool_failure(self, monkeypatch):
        """A model can emit invalid JSON. That is its mistake to see and fix,
        not a reason to throw away the rounds already paid for."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        self._http(monkeypatch, [
            self._payload({"role": "assistant", "content": None,
                           "tool_calls": [self._call(arguments="{not json")]},
                          finish="tool_calls"),
            self._payload({"role": "assistant", "content": "recovered"}),
        ], cap)
        done = providers.complete(
            providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p",
            tools=(_tool(),), run_tool=lambda n, a: "never reached")
        assert done.text == "recovered"
        assert "not valid JSON" in done.tool_calls[0].result

    def test_tokens_accumulate_over_every_round(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        self._http(monkeypatch, [
            self._payload({"role": "assistant", "content": None,
                           "tool_calls": [self._call()]}, finish="tool_calls"),
            self._payload({"role": "assistant", "content": "ok"}),
        ], cap)
        done = providers.complete(
            providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p",
            tools=(_tool(),), run_tool=lambda n, a: "r")
        assert (done.input_tokens, done.output_tokens) == (20, 40)

    def test_the_last_round_withholds_the_tools(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        rounds = providers.MAX_TOOL_ROUNDS
        self._http(monkeypatch, [
            self._payload({"role": "assistant", "content": None,
                           "tool_calls": [self._call(cid=f"c{i}")]}, finish="tool_calls")
            for i in range(rounds)
        ] + [self._payload({"role": "assistant", "content": "forced answer"})], cap)
        done = providers.complete(
            providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p",
            tools=(_tool(),), run_tool=lambda n, a: "r")
        assert done.text == "forced answer"
        assert "tools" in cap["bodies"][0] and "tools" not in cap["bodies"][-1]

    def test_an_endpoint_that_rejects_tools_says_what_to_do(self, monkeypatch):
        """Small local models behind ollama often have no tool support, and a
        bare 'API error 400' sends the operator to their prompt instead."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        _fake_http(monkeypatch, error=urllib.error.HTTPError(
            "u", 400, "Bad Request", {}, io.BytesIO(b'{"error":{"message":"no tools"}}')))
        with pytest.raises(providers.ProviderError, match="--no-tools"):
            providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat",
                               "s", "p", tools=(_tool(),), run_tool=lambda n, a: "")

    def test_it_offers_switching_provider_before_dropping_the_tools(self, monkeypatch):
        """Regression: the message named only --no-tools, which answers from
        the snapshot alone — for a question that asked about history, that is
        not the answer wanted. The escape listed first gets taken first."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        _fake_http(monkeypatch, error=urllib.error.HTTPError(
            "u", 400, "Bad Request", {}, io.BytesIO(b'{"error":{"message":"no tools"}}')))
        with pytest.raises(providers.ProviderError) as e:
            providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat",
                               "s", "p", tools=(_tool(),), run_tool=lambda n, a: "")
        text = str(e.value)
        assert f"--provider {providers.DEFAULT_PROVIDER}" in text
        assert text.index("--provider") < text.index("--no-tools")
        assert "warehouse" in text, "say what switching buys"

    def test_the_providers_own_diagnosis_is_passed_through(self, monkeypatch):
        """The API knows why it refused — an OpenAI reasoning model names the
        endpoint and the setting. Replacing that with something vaguer of our
        own would throw away the only accurate part of the message."""
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        detail = (b'{"error":{"message":"Function tools with reasoning_effort are not '
                  b'supported for gpt-5.6-luna in /v1/chat/completions."}}')
        _fake_http(monkeypatch, error=urllib.error.HTTPError(
            "u", 400, "Bad Request", {}, io.BytesIO(detail)))
        with pytest.raises(providers.ProviderError, match="reasoning_effort"):
            providers.complete(providers.PROVIDERS["openai"], "gpt-5.6-luna",
                               "s", "p", tools=(_tool(),), run_tool=lambda n, a: "")

    def test_the_chat_completions_path_sends_no_reasoning_effort(self, monkeypatch):
        """Only OpenAI's Responses path carries effort. DeepSeek and the rest
        take no such field, and inventing one would 400 them."""
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        cap = {}
        self._http(monkeypatch, [self._payload({"content": "hi"})], cap)
        providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p",
                           effort="high", tools=(_tool(),), run_tool=lambda n, a: "")
        assert "reasoning_effort" not in cap["bodies"][0]
        assert "reasoning" not in cap["bodies"][0]

    def test_a_400_without_tools_does_not_blame_tools(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "k")
        _fake_http(monkeypatch, error=urllib.error.HTTPError(
            "u", 400, "Bad Request", {}, io.BytesIO(b'{"error":{"message":"nope"}}')))
        with pytest.raises(providers.ProviderError) as e:
            providers.complete(providers.PROVIDERS["deepseek"], "deepseek-chat", "s", "p")
        assert "--no-tools" not in str(e.value)


class TestOpenAIResponsesTransport:
    """OpenAI's Responses API. Shapes here mirror what the live API returned
    when this was validated against gpt-5.6-luna, not what docs imply."""

    def _fc(self, call_id="call_abc", name="query_warehouse",
            arguments='{"sql": "SELECT 1"}'):
        return {"type": "function_call", "id": "fc_xyz", "call_id": call_id,
                "name": name, "arguments": arguments, "status": "completed"}

    def _msg(self, text="the answer"):
        return {"type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text}]}

    def _payload(self, output, status="completed"):
        return {"model": "gpt-5.6-luna", "status": status, "output": output,
                "usage": {"input_tokens": 10, "output_tokens": 20}}

    def _http(self, monkeypatch, payloads, capture):
        bodies = []

        def urlopen(req, timeout=None):
            bodies.append(json.loads(req.data.decode()))
            capture["bodies"] = bodies
            capture["url"] = req.full_url
            payload = payloads[len(bodies) - 1]

            class R:
                def read(self): return json.dumps(payload).encode()
                def __enter__(self): return self
                def __exit__(self, *a): return False
            return R()
        monkeypatch.setattr(providers.urllib.request, "urlopen", urlopen)
        return bodies

    def _complete(self, monkeypatch, payloads, cap, **kw):
        monkeypatch.setenv("OPENAI_API_KEY", "k")
        self._http(monkeypatch, payloads, cap)
        return providers.complete(providers.PROVIDERS["openai"], "gpt-5.6-luna",
                                  "SYS", "PROMPT", **kw)

    def test_openai_uses_the_responses_endpoint(self, monkeypatch):
        cap = {}
        self._complete(monkeypatch, [self._payload([self._msg()])], cap)
        assert cap["url"].endswith("/responses")

    def test_the_system_prompt_travels_as_instructions(self, monkeypatch):
        """Losing it would silently drop every analyst rule — no-advice, never
        treat n/a as zero, the untrusted-input rule — and read as the model
        being bad rather than the request being wrong."""
        cap = {}
        self._complete(monkeypatch, [self._payload([self._msg()])], cap)
        assert cap["bodies"][0]["instructions"] == "SYS"

    def test_effort_reaches_this_provider(self, monkeypatch):
        """It was accepted and silently ignored on chat-completions."""
        cap = {}
        self._complete(monkeypatch, [self._payload([self._msg()])], cap,
                       effort="xhigh")
        assert cap["bodies"][0]["reasoning"] == {"effort": "xhigh"}

    def test_the_conversation_is_not_stored(self, monkeypatch):
        """Left at its default the API retains it, leaving the snapshot on
        someone else's server after the request that needed it."""
        cap = {}
        self._complete(monkeypatch, [self._payload([self._msg()])], cap)
        assert cap["bodies"][0]["store"] is False

    def test_tools_are_declared_flat(self, monkeypatch):
        """No nested "function" object here, unlike chat-completions."""
        cap = {}
        self._complete(monkeypatch, [self._payload([self._msg()])], cap,
                       tools=(_tool(),), run_tool=lambda n, a: "")
        spec = cap["bodies"][0]["tools"][0]
        assert spec["type"] == "function" and spec["name"] == "query_warehouse"
        assert "function" not in spec

    def test_a_tool_call_round_trips(self, monkeypatch):
        cap = {}
        ran = []
        done = self._complete(monkeypatch, [
            self._payload([{"type": "reasoning", "id": "rs_1"}, self._fc()]),
            self._payload([self._msg("4,696 rows")]),
        ], cap, tools=(_tool(),),
            run_tool=lambda n, a: ran.append((n, a)) or "days\n4696")
        assert done.text == "4,696 rows"
        assert ran == [("query_warehouse", {"sql": "SELECT 1"})]
        assert done.tool_calls[0].result == "days\n4696"

    def test_the_result_is_keyed_by_call_id_not_id(self, monkeypatch):
        """The item carries both and only call_id matches a result to its
        call. Using `id` looks right and silently fails to pair."""
        cap = {}
        self._complete(monkeypatch, [
            self._payload([self._fc(call_id="call_REAL")]),
            self._payload([self._msg()]),
        ], cap, tools=(_tool(),), run_tool=lambda n, a: "ROWS")
        sent = cap["bodies"][1]["input"][-1]
        assert sent["type"] == "function_call_output"
        assert sent["call_id"] == "call_REAL" and sent["output"] == "ROWS"

    def test_reasoning_items_are_echoed_back(self, monkeypatch):
        """Same rule as the Anthropic loop: what preceded a tool call has to
        travel back with it."""
        cap = {}
        reasoning = {"type": "reasoning", "id": "rs_1", "summary": []}
        self._complete(monkeypatch, [
            self._payload([reasoning, self._fc()]),
            self._payload([self._msg()]),
        ], cap, tools=(_tool(),), run_tool=lambda n, a: "r")
        assert reasoning in cap["bodies"][1]["input"]

    def test_tokens_accumulate_over_rounds(self, monkeypatch):
        cap = {}
        done = self._complete(monkeypatch, [
            self._payload([self._fc()]),
            self._payload([self._msg()]),
        ], cap, tools=(_tool(),), run_tool=lambda n, a: "r")
        assert (done.input_tokens, done.output_tokens) == (20, 40)

    def test_the_last_round_withholds_the_tools(self, monkeypatch):
        cap = {}
        rounds = providers.MAX_TOOL_ROUNDS
        done = self._complete(monkeypatch,
            [self._payload([self._fc(call_id=f"c{i}")]) for i in range(rounds)]
            + [self._payload([self._msg("forced")])],
            cap, tools=(_tool(),), run_tool=lambda n, a: "r")
        assert done.text == "forced"
        assert "tools" in cap["bodies"][0] and "tools" not in cap["bodies"][-1]

    def test_malformed_arguments_come_back_as_a_tool_failure(self, monkeypatch):
        cap = {}
        done = self._complete(monkeypatch, [
            self._payload([self._fc(arguments="{not json")]),
            self._payload([self._msg("recovered")]),
        ], cap, tools=(_tool(),), run_tool=lambda n, a: "never reached")
        assert done.text == "recovered"
        assert "not valid JSON" in done.tool_calls[0].result

    def test_text_is_walked_out_of_the_output_items(self, monkeypatch):
        """There is no top-level `output_text` on this endpoint. Reaching for
        one returns nothing and presents as an empty-response bug."""
        cap = {}
        done = self._complete(monkeypatch, [self._payload(
            [{"type": "reasoning", "id": "r"},
             {"type": "message", "content": [
                 {"type": "output_text", "text": "one"},
                 {"type": "output_text", "text": "two"}]}])], cap)
        assert done.text == "one\ntwo"

    def test_a_refusal_is_reported_not_returned_as_text(self, monkeypatch):
        cap = {}
        with pytest.raises(providers.ProviderError, match="declined"):
            self._complete(monkeypatch, [self._payload(
                [{"type": "message",
                  "content": [{"type": "refusal", "refusal": "no"}]}])], cap)

    def test_an_incomplete_response_says_why(self, monkeypatch):
        cap = {}
        payload = self._payload([{"type": "reasoning", "id": "r"}],
                                status="incomplete")
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
        with pytest.raises(providers.ProviderError, match="max_output_tokens"):
            self._complete(monkeypatch, [payload], cap)

    def test_no_tools_still_answers(self, monkeypatch):
        cap = {}
        done = self._complete(monkeypatch, [self._payload([self._msg("plain")])], cap)
        assert done.text == "plain" and done.tool_calls == ()
        assert "tools" not in cap["bodies"][0]
