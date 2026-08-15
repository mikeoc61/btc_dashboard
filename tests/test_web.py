"""The local web view: caching, the ask guard, and the money-spending bind."""
from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient          # noqa: E402

from btc_dashboard import analyst, snapshot, web    # noqa: E402
from btc_dashboard.config import Config             # noqa: E402


@pytest.fixture
def built(monkeypatch):
    """Count collections so the caching can be observed."""
    calls = {"n": 0}

    def build(cfg, only=None, *, refresh=False):
        calls["n"] += 1
        return {"schema_version": 1, "generated_at": "2026-08-15T01:53:00+00:00",
                "asset": "btc", "sources": {}}

    monkeypatch.setattr(snapshot, "build", build)
    monkeypatch.setattr(web.snapshot, "build", build)
    return calls


@pytest.fixture
def client(built, tmp_path):
    return TestClient(web.create_app(Config.from_env(cache_dir=tmp_path)))


class TestSnapshotIsNotRebuiltPerRequest:
    def test_repeated_page_loads_collect_once(self, client, built):
        for _ in range(5):
            assert client.get("/").status_code == 200
        assert built["n"] == 1, "an auto-refreshing tab must not scrape every load"

    def test_expiry_rebuilds(self, built, tmp_path):
        """Past the TTL the next request collects again."""
        cache = web.SnapshotCache(Config.from_env(cache_dir=tmp_path), ttl=0.0)
        cache.get()
        cache.get()
        assert built["n"] == 2

    def test_explicit_refresh_recollects(self, client, built):
        client.get("/")
        client.post("/refresh", follow_redirects=False)
        assert built["n"] == 2

    def test_refresh_redirects_rather_than_rendering(self, client):
        r = client.post("/refresh", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"


class TestAsk:
    def _answer(self, monkeypatch, **over):
        result = analyst.AnalystResult(
            text=over.get("text", "an answer"), error=over.get("error"),
            model="m", provider="p", input_tokens=1, output_tokens=2,
        )
        monkeypatch.setattr(web.analyst, "ask", lambda *a, **k: result)

    def test_answer_appears_on_the_page_after_a_redirect(self, client, monkeypatch):
        self._answer(monkeypatch)
        r = client.post("/ask", data={"q": "why is vol low?"}, follow_redirects=False)
        assert r.status_code == 303, "POST must redirect so reload cannot resubmit"

        page = client.get("/").text
        assert "an answer" in page and "why is vol low?" in page
        assert "p/m" in page and "1 in / 2 out" in page, "cost must be visible"

    def test_a_second_question_inside_the_cooldown_is_not_sent(self, client, monkeypatch):
        sent = {"n": 0}

        def counting_ask(*a, **k):
            sent["n"] += 1
            return analyst.AnalystResult(text="ok", provider="p", model="m")

        monkeypatch.setattr(web.analyst, "ask", counting_ask)
        client.post("/ask", data={"q": "first"})
        client.post("/ask", data={"q": "second"})

        assert sent["n"] == 1, "a double-submit must not cost twice"
        assert "costs money" in client.get("/").text

    def test_an_empty_question_is_not_sent(self, client, monkeypatch):
        sent = {"n": 0}
        monkeypatch.setattr(
            web.analyst, "ask",
            lambda *a, **k: (sent.__setitem__("n", sent["n"] + 1),
                             analyst.AnalystResult(text="x"))[1])
        client.post("/ask", data={"q": "   "})
        assert sent["n"] == 0

    def test_an_analyst_error_is_shown_not_swallowed(self, client, monkeypatch):
        self._answer(monkeypatch, text=None, error="OPENAI_API_KEY is not set")
        client.post("/ask", data={"q": "x"})
        assert "OPENAI_API_KEY is not set" in client.get("/").text

    def test_the_answer_is_escaped(self, client, monkeypatch):
        self._answer(monkeypatch, text="<script>alert(1)</script>")
        client.post("/ask", data={"q": "x"})
        page = client.get("/").text
        assert "<script>alert(1)" not in page and "&lt;script&gt;" in page

    def test_the_question_is_escaped(self, client, monkeypatch):
        self._answer(monkeypatch)
        client.post("/ask", data={"q": "<img src=x onerror=alert(1)>"})
        page = client.get("/").text
        assert "<img src=x" not in page and "&lt;img" in page


class TestBindDefault:
    """This process holds the provider key and /ask spends money."""

    def test_default_host_is_loopback(self, monkeypatch):
        seen = {}
        monkeypatch.setitem(
            __import__("sys").modules, "uvicorn",
            type("U", (), {"run": staticmethod(
                lambda app, **kw: seen.update(kw))}))
        web.main([])
        assert seen["host"] == "127.0.0.1"

    def test_a_wider_bind_warns(self, capsys, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                            type("U", (), {"run": staticmethod(lambda *a, **k: None)}))
        web.main(["--host", "0.0.0.0"])
        out = capsys.readouterr().out
        assert "WARNING" in out and "spends money" in out
        assert "ssh -L" in out, "the warning should show the safe alternative"

    def test_loopback_does_not_warn(self, capsys, monkeypatch):
        monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                            type("U", (), {"run": staticmethod(lambda *a, **k: None)}))
        web.main([])
        assert "WARNING" not in capsys.readouterr().out
