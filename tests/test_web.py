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


class TestTheDataUpdatesWithoutDisturbingTheAskBox:
    """The page used to reload whole. A tick landing while someone typed a
    question replaced the field and took the question with it."""

    def test_the_page_updates_from_the_fragment_rather_than_reloading(self, client):
        body = client.get("/").text
        assert web.LIVE_PATH in body and "setInterval" in body
        assert '<noscript><meta http-equiv="refresh"' in body

    def test_the_fragment_has_no_ask_box(self, client):
        """What a tick writes into the page. A control here is the bug."""
        live = client.get(web.LIVE_PATH)
        assert live.status_code == 200
        assert "<form" not in live.text and "<input" not in live.text

    def test_the_fragment_carries_the_data_regions(self, client):
        live = client.get(web.LIVE_PATH).text
        from btc_dashboard import html as page
        for ident in page.LIVE_IDS:
            assert f'id="{ident}"' in live

    def test_polling_collects_nothing(self, client, built):
        """A tab open all day must not turn into a scrape every minute."""
        client.get("/")
        for _ in range(10):
            client.get(web.LIVE_PATH)
        assert built["n"] == 1

    def test_an_answer_is_not_touched_by_a_poll(self, client, monkeypatch):
        monkeypatch.setattr(
            web.analyst, "ask",
            lambda *a, **k: analyst.AnalystResult(
                text="an answer", provider="p", model="m",
                input_tokens=1, output_tokens=2))
        client.post("/ask", data={"q": "why is vol low?"})
        assert "an answer" not in client.get(web.LIVE_PATH).text
        assert "an answer" in client.get("/").text


class TestBindDefault:
    """This process holds the provider key and /ask spends money."""

    def test_default_host_is_loopback(self, monkeypatch):
        seen = {}
        # Stubbed: otherwise this depends on whether the developer's machine
        # happens to have 8001 free — an SSH tunnel forwarding it is enough to
        # make the real check refuse and the test fail for the wrong reason.
        monkeypatch.setattr(web, "_port_free", lambda h, p: True)
        monkeypatch.setitem(
            __import__("sys").modules, "uvicorn",
            type("U", (), {"run": staticmethod(
                lambda app, **kw: seen.update(kw))}))
        web.main([])
        assert seen["host"] == "127.0.0.1"

    def test_a_wider_bind_warns(self, capsys, monkeypatch):
        monkeypatch.setattr(web, "_port_free", lambda h, p: True)
        monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                            type("U", (), {"run": staticmethod(lambda *a, **k: None)}))
        web.main(["--host", "0.0.0.0"])
        out = capsys.readouterr().out
        assert "WARNING" in out and "spends money" in out
        assert "ssh -L" in out, "the warning should show the safe alternative"

    def test_loopback_does_not_warn(self, capsys, monkeypatch):
        monkeypatch.setattr(web, "_port_free", lambda h, p: True)
        monkeypatch.setitem(__import__("sys").modules, "uvicorn",
                            type("U", (), {"run": staticmethod(lambda *a, **k: None)}))
        web.main([])
        assert "WARNING" not in capsys.readouterr().out


class TestPortSelection:
    """Two local dashboards on one host must not collide by default."""

    def test_default_port_avoids_the_peer_monitor(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(web, "_port_free", lambda h, p: True)
        monkeypatch.setitem(
            __import__("sys").modules, "uvicorn",
            type("U", (), {"run": staticmethod(lambda app, **kw: seen.update(kw))}))
        web.main([])
        assert seen["port"] == web.DEFAULT_PORT
        assert web.DEFAULT_PORT != 8000, "8000 is peer_monitor's"

    def test_a_taken_port_explains_itself(self, capsys, monkeypatch):
        monkeypatch.setattr(web, "_port_free", lambda h, p: False)
        monkeypatch.setitem(
            __import__("sys").modules, "uvicorn",
            type("U", (), {"run": staticmethod(lambda *a, **k: None)}))

        assert web.main(["--port", "8000"]) == 1
        out = capsys.readouterr().out
        assert "already in use" in out
        assert "peer_monitor" in out, "name the likely culprit"
        assert "--port 8001" in out, "offer the fix"

    def test_a_free_port_starts(self, monkeypatch):
        started = {}
        monkeypatch.setattr(web, "_port_free", lambda h, p: True)
        monkeypatch.setitem(
            __import__("sys").modules, "uvicorn",
            type("U", (), {"run": staticmethod(lambda app, **kw: started.update(kw))}))
        assert web.main([]) == 0
        assert started

    def test_port_free_detects_a_live_listener(self):
        """Exercised against a real socket, not a stub."""
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            s.listen(1)
            taken = s.getsockname()[1]
            assert web._port_free("127.0.0.1", taken) is False
        assert web._port_free("127.0.0.1", taken) is True


class TestCloseDateAttribution:
    """The two providers date a daily bar by opposite conventions."""

    def test_coingecko_points_belong_to_the_day_that_ended(self, monkeypatch):
        """A point stamped 16 Aug 00:00 is the close of 15 Aug. Reading it as
        the 16th dates every close a day late."""
        import datetime
        from btc_dashboard.sources import price

        def fake(url, timeout):
            base = datetime.datetime(2026, 8, 16, tzinfo=datetime.timezone.utc)
            return {"prices": [
                [int((base - datetime.timedelta(days=1)).timestamp() * 1000), 62996.4],
                [int(base.timestamp() * 1000), 63031.05],
                [int((base + datetime.timedelta(hours=1, minutes=47)).timestamp() * 1000),
                 63011.9],
            ]}

        monkeypatch.setattr(price, "_get", fake)
        series = price._coingecko(10)
        assert series[0][0] == datetime.date(2026, 8, 14)
        assert series[1][0] == datetime.date(2026, 8, 15)
        assert series[2][0] == datetime.date(2026, 8, 16)   # today, in progress

    def test_binance_klines_are_dated_by_their_open(self, monkeypatch):
        """The opposite: a kline opening on 15 Aug carries 15 Aug's close."""
        import datetime
        from btc_dashboard.sources import price

        day = datetime.datetime(2026, 8, 15, tzinfo=datetime.timezone.utc)
        monkeypatch.setattr(price, "_get", lambda u, t: [
            [int(day.timestamp() * 1000), "1", "2", "3", "62955.3", "0"],
        ])
        assert price._binance(10)[0] == (datetime.date(2026, 8, 15), 62955.3)

    def test_collect_reports_the_reference_date(self, monkeypatch):
        import datetime
        from btc_dashboard.sources import price
        from btc_dashboard.config import Config

        base = datetime.datetime(2026, 8, 16, tzinfo=datetime.timezone.utc)
        points = [
            [int((base - datetime.timedelta(days=i)).timestamp() * 1000), 63000.0 + i]
            for i in range(3, -1, -1)
        ]
        # The live quote lands mid-day, not on the midnight boundary. Putting a
        # fake one at 00:00 makes the last completed close look like the live
        # one and shifts the whole attribution by a day.
        points.append([int((base + datetime.timedelta(hours=1, minutes=47))
                           .timestamp() * 1000), 63011.9])
        monkeypatch.setattr(price, "_get", lambda u, t: {"prices": points})

        data = price.collect(Config.from_env()).data
        assert data["prev_close_date"] == "2026-08-15"
        assert data["spot"] == 63011.9
