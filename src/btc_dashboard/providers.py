"""LLM providers for `--ask`.

Client-side only, like everything analyst-related: a provider is selected on
the operator's machine and called with the operator's credential. A deployed
snapshot service imports none of this.

Two wire protocols cover every provider here. Anthropic has its own Messages
API and gets the official SDK, which knows about thinking budgets, reasoning
effort, and the refusal stop reason. Everything else speaks the OpenAI
chat-completions shape, which is handled with stdlib HTTP rather than a second
SDK dependency — the request is a JSON POST and the response is one field deep,
so a client library would earn nothing.

Selecting one
-------------
A model spec may name its provider:

    claude-opus-5                 -> the default provider
    anthropic/claude-opus-5       -> explicit
    deepseek/deepseek-chat
    ollama/llama3                 -> local, no credential

`--provider` sets it explicitly and a `provider/` prefix overrides that, so
`BTC_DASHBOARD_MODEL` can carry both in one variable for a scheduled run.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import config

# LLM calls are slow and are not the per-source network fetches, so they do not
# share the collection timeout.
TIMEOUT = 120
MAX_TOKENS = 16000
# How many times the model may call a tool before it must answer with what it
# has. A bound rather than a budget: each round is a paid request, and a model
# stuck rewriting a failing query would otherwise loop until the timeout. The
# last round drops the tools rather than erroring, so the caller always gets a
# real answer — one that had fewer facts than the model wanted, which it is
# told to say.
MAX_TOOL_ROUNDS = 8


@dataclass(frozen=True)
class Provider:
    name: str
    kind: str                       # "anthropic" | "openai"
    base_url: str
    env_key: str | None             # None = no credential required
    default_model: str | None       # None = the user must name one
    supports_effort: bool = False
    supports_max_tokens: bool = True


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider(
        name="anthropic", kind="anthropic",
        base_url="https://api.anthropic.com",
        env_key="ANTHROPIC_API_KEY",
        default_model="claude-opus-5",
        supports_effort=True,
    ),
    "openai": Provider(
        name="openai", kind="openai",
        base_url="https://api.openai.com/v1",
        env_key="OPENAI_API_KEY",
        # Deliberately none: OpenAI's model ids move faster than this file, and
        # a stale default fails as a confusing 404 rather than a clear message.
        default_model=None,
        # Newer models reject `max_tokens` in favour of a different field, so
        # the cap is left to the API's own default rather than guessed at.
        supports_max_tokens=False,
    ),
    "deepseek": Provider(
        name="deepseek", kind="openai",
        base_url="https://api.deepseek.com/v1",
        env_key="DEEPSEEK_API_KEY",
        default_model="deepseek-chat",
    ),
    "openrouter": Provider(
        name="openrouter", kind="openai",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        default_model=None,
    ),
    "ollama": Provider(
        name="ollama", kind="openai",
        # Local models keep the snapshot on the machine that collected it,
        # which is the only option here that involves no third party at all.
        base_url=os.environ.get("OLLAMA_HOST", "http://localhost:11434") + "/v1",
        env_key=None,
        default_model=None,
    ),
}

DEFAULT_PROVIDER = "anthropic"


class ProviderError(RuntimeError):
    """Configuration or transport failure, phrased for the operator."""


def resolve(model_spec: str | None, provider_name: str | None = None) -> tuple[Provider, str]:
    """Turn a model spec and optional provider into `(Provider, model_id)`."""
    spec = (model_spec or "").strip()
    name = (provider_name or DEFAULT_PROVIDER).strip()

    # A `provider/` prefix wins over --provider, so one env var can carry both.
    if "/" in spec:
        prefix, rest = spec.split("/", 1)
        if prefix in PROVIDERS:
            name, spec = prefix, rest.strip()
        # Anything else keeps its slash: OpenRouter model ids contain one.

    provider = PROVIDERS.get(name)
    if provider is None:
        raise ProviderError(
            f"unknown provider {name!r} — available: {', '.join(sorted(PROVIDERS))}"
        )

    model = spec or provider.default_model
    if not model:
        raise ProviderError(
            f"{provider.name} has no default model — name one, "
            f"e.g. --model {provider.name}/<model-id>"
        )
    return provider, model


def api_key(provider: Provider, env_file: Path | None = None) -> str | None:
    """The provider's credential from the environment, else from an env file.

    The file fallback exists because a scheduled run starts without a login
    shell, so nothing from a profile is exported. Providers needing no
    credential return None and are still usable.
    """
    if provider.env_key is None:
        return None
    # Idempotent, and a no-op for anything already in the real environment.
    # Called here too so a library caller that never builds a Config still
    # picks the file up.
    config.load_env_file(env_file)
    return os.environ.get(provider.env_key) or None


@dataclass(frozen=True)
class ToolCall:
    """One tool invocation and what came back.

    Kept and returned rather than discarded once the model has read it: the
    operator paid for it, and a conclusion drawn from a query nobody can see is
    not checkable. The caller shows these.
    """

    name: str
    arguments: dict
    result: str


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    # Every round, in order. Empty when no tools were offered or none were used.
    tool_calls: tuple[ToolCall, ...] = ()


def complete(provider: Provider, model: str, system: str, prompt: str,
             effort: str = "high", timeout: int = TIMEOUT,
             tools: "tuple" = (), run_tool=None) -> Completion:
    """Send a prompt, running any tools the model asks for, and return the answer.

    With no `tools` this is one request, as it always was. With tools it is a
    loop: the model may ask for a tool, `run_tool(name, arguments) -> str`
    supplies the result, and the conversation continues until the model answers
    or `MAX_TOOL_ROUNDS` is reached. Token counts accumulate across every round,
    so the reported cost is the cost of the whole exchange rather than of the
    last leg of it.

    `run_tool` must not raise. A tool failure is something the model can see and
    correct on its next turn, so it belongs in the tool's result text; an
    exception here would throw away the rounds already paid for.

    Raises ProviderError with an operator-facing message.
    """
    if provider.kind == "anthropic":
        return _anthropic(provider, model, system, prompt, effort, timeout,
                          tools, run_tool)
    return _openai(provider, model, system, prompt, timeout, tools, run_tool)


def _anthropic(provider, model, system, prompt, effort, timeout,
               tools=(), run_tool=None) -> Completion:
    try:
        import anthropic
    except ImportError:
        raise ProviderError("the anthropic SDK is not installed — pip install anthropic")

    if not api_key(provider):
        raise ProviderError(_missing_key(provider))

    spec = [
        {"name": t.name, "description": t.description, "input_schema": t.parameters}
        for t in tools
    ]
    messages = [{"role": "user", "content": prompt}]
    calls: list[ToolCall] = []
    client = anthropic.Anthropic(timeout=timeout)
    in_tokens = out_tokens = 0

    for round_number in range(MAX_TOOL_ROUNDS + 1):
        kwargs = {
            "model": model,
            # Thinking is on by default on current models and max_tokens caps
            # thinking plus response together, so this sits well above the answer
            # length to keep a long deliberation from truncating the text.
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": messages,
        }
        if provider.supports_effort:
            kwargs["output_config"] = {"effort": effort}
        # The final round offers no tools, which is what forces an answer out of
        # a model that would otherwise keep querying. Without this the loop ends
        # on a tool_use turn that carries no text at all.
        if spec and round_number < MAX_TOOL_ROUNDS:
            kwargs["tools"] = spec

        try:
            resp = client.messages.create(**kwargs)
        except anthropic.AuthenticationError:
            raise ProviderError(f"{provider.env_key} was rejected")
        except anthropic.NotFoundError:
            raise ProviderError(f"unknown model: {model}")
        except anthropic.RateLimitError as e:
            retry = e.response.headers.get("retry-after", "?") if e.response else "?"
            raise ProviderError(f"rate limited — retry after {retry}s")
        except anthropic.APIStatusError as e:
            raise ProviderError(f"API error {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            raise ProviderError("could not reach the Anthropic API")

        in_tokens += resp.usage.input_tokens or 0
        out_tokens += resp.usage.output_tokens or 0

        # Checked before reading content: a refusal returns HTTP 200 with content
        # empty or partial, so indexing into it would break here.
        if resp.stop_reason == "refusal":
            cat = getattr(resp.stop_details, "category", None) if resp.stop_details else None
            raise ProviderError(f"request declined by safety classifiers ({cat})")

        if resp.stop_reason == "tool_use" and run_tool is not None:
            # The whole content list goes back, not just the tool_use blocks:
            # with thinking enabled the API requires the thinking blocks that
            # preceded the call to come with it.
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                arguments = dict(block.input or {})
                text = run_tool(block.name, arguments)
                calls.append(ToolCall(block.name, arguments, text))
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": text,
                })
            messages.append({"role": "user", "content": results})
            continue

        text = "\n".join(b.text for b in resp.content if b.type == "text").strip()
        if not text:
            raise ProviderError(f"empty response (stop_reason={resp.stop_reason})")
        return Completion(text, resp.model, in_tokens, out_tokens, tuple(calls))

    # Unreachable: the final round sends no tools, so it cannot stop on one.
    raise ProviderError("the model kept calling tools without answering")


def _openai(provider, model, system, prompt, timeout,
            tools=(), run_tool=None) -> Completion:
    key = api_key(provider)
    if key is None and provider.env_key is not None:
        raise ProviderError(_missing_key(provider))

    spec = [
        {"type": "function", "function": {
            "name": t.name, "description": t.description, "parameters": t.parameters,
        }}
        for t in tools
    ]
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]
    calls: list[ToolCall] = []
    headers = {"content-type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    in_tokens = out_tokens = 0
    seen_usage = False
    last_model = model

    for round_number in range(MAX_TOOL_ROUNDS + 1):
        body = {"model": model, "messages": messages}
        if provider.supports_max_tokens:
            body["max_tokens"] = MAX_TOKENS
        # As with the Anthropic loop: the last round withholds the tools so the
        # model has to answer rather than ask again.
        if spec and round_number < MAX_TOOL_ROUNDS:
            body["tools"] = spec

        req = urllib.request.Request(
            f"{provider.base_url}/chat/completions",
            data=json.dumps(body).encode(), headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = json.load(r)
        except urllib.error.HTTPError as e:
            raise ProviderError(_http_error(provider, e, tools=bool(spec)))
        except urllib.error.URLError as e:
            raise ProviderError(
                f"could not reach {provider.name} at {provider.base_url}: {e.reason}")
        except (TimeoutError, OSError) as e:
            raise ProviderError(f"{provider.name} request failed: {e}")

        try:
            choice = payload["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError):
            raise ProviderError(f"{provider.name} returned an unexpected response shape")

        usage = payload.get("usage") or {}
        if usage:
            seen_usage = True
            in_tokens += usage.get("prompt_tokens") or 0
            out_tokens += usage.get("completion_tokens") or 0
        last_model = payload.get("model") or last_model

        requested = message.get("tool_calls") or []
        if requested and run_tool is not None:
            # Echoed back verbatim: the API matches each result to the call by
            # id, and a reconstructed message loses fields some providers need.
            messages.append(message)
            for call in requested:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                # Arguments arrive as a JSON *string*, and a model can emit a
                # malformed one. That is the model's mistake to see and fix, so
                # it comes back as a tool result rather than an exception.
                try:
                    arguments = json.loads(fn.get("arguments") or "{}")
                except ValueError as e:
                    arguments, text = {}, f"TOOL FAILED: arguments were not valid JSON: {e}"
                else:
                    text = run_tool(name, arguments)
                calls.append(ToolCall(name, arguments, text))
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": text,
                })
            continue

        text = (message.get("content") or "").strip()
        if not text:
            # Reasoning models can spend the whole budget thinking and return an
            # empty message; some expose the reasoning separately.
            text = (message.get("reasoning_content") or "").strip()
        if not text:
            raise ProviderError(
                f"empty response from {provider.name} "
                f"(finish_reason={choice.get('finish_reason')})"
            )

        return Completion(
            text=text,
            model=last_model,
            input_tokens=in_tokens if seen_usage else None,
            output_tokens=out_tokens if seen_usage else None,
            tool_calls=tuple(calls),
        )

    raise ProviderError("the model kept calling tools without answering")


def _missing_key(provider: Provider) -> str:
    return (
        f"{provider.env_key} is not set — export it, or put it in "
        f"{config.default_config_dir() / 'env'}"
    )


def _http_error(provider: Provider, e: urllib.error.HTTPError,
                tools: bool = False) -> str:
    detail = ""
    try:
        body = json.loads(e.read().decode())
        detail = (body.get("error") or {}).get("message") or ""
    except Exception:
        pass
    if e.code == 401:
        return f"{provider.env_key} was rejected by {provider.name}"
    if e.code == 404:
        return f"{provider.name} does not know that model{': ' + detail if detail else ''}"
    if e.code == 429:
        return f"{provider.name} rate limited{': ' + detail if detail else ''}"
    if e.code == 400 and tools:
        # Not every OpenAI-shaped endpoint takes tools — a small local model
        # behind ollama often does not — and a bare "API error 400" sends the
        # operator looking at their prompt instead of their model choice.
        return (
            f"{provider.name} rejected the request{': ' + detail if detail else ''}. "
            f"Tools were offered; if this model does not support them, run with "
            f"--no-tools."
        )
    return f"{provider.name} API error {e.code}{': ' + detail if detail else ''}"
