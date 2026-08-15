"""A local web view of the dashboard, with the analyst box.

Optional: `pip install -e ".[web]"`, then `btc-dashboard-web`.

**This process holds your provider key.** Everything else in this tool keeps
the LLM strictly client-side — `analyst.py` runs on the operator's machine and
a snapshot service holds no credential. An ask box in a browser cannot work
that way: the server has to make the call. That is fine when the server *is*
your own machine reached over an SSH tunnel, which is the intended deployment,
and it is why:

- the default bind is 127.0.0.1, never 0.0.0.0. On 0.0.0.0 anyone who can
  reach the port can spend your API budget;
- `/ask` has a cooldown, because each request costs real money and a
  double-submit should not cost twice;
- the token count of every answer is shown, so the cost is visible rather than
  invisible.

Collection is decoupled from HTTP entirely. The page holds a snapshot in
memory with a short TTL, so an auto-refreshing tab does not scrape Farside or
poll CoinGecko once a minute, and asking three questions costs three LLM calls
and zero collections.
"""
from __future__ import annotations

import threading
import time

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse

from . import analyst, html as page, snapshot
from .config import Config

# How long an in-memory snapshot serves before it is rebuilt. Short, because
# price and node are live tip state; long enough that a 60-second page refresh
# does not translate into a collection every 60 seconds.
SNAPSHOT_TTL = 120
# Minimum gap between analyst calls. Guards a double-submit and a stuck client,
# not a determined human — this is a single-user local page, not a public API.
ASK_COOLDOWN = 5.0
PAGE_REFRESH = 60
# Not 8000: bitcoin_peer_monitor conventionally takes that, and two local
# dashboards on one host should not fight over a port by default.
DEFAULT_PORT = 8001


class SnapshotCache:
    """One snapshot shared by every request, rebuilt on a timer.

    Locked because a browser fetching favicons and pages concurrently would
    otherwise trigger several simultaneous collections, each opening the
    warehouse and scraping upstream.
    """

    def __init__(self, cfg: Config, ttl: float = SNAPSHOT_TTL):
        self.cfg, self.ttl = cfg, ttl
        self._lock = threading.Lock()
        self._snapshot: dict | None = None
        self._at = 0.0

    def get(self, *, refresh: bool = False) -> dict:
        with self._lock:
            fresh_enough = (
                self._snapshot is not None and time.monotonic() - self._at < self.ttl
            )
            if fresh_enough and not refresh:
                return self._snapshot
            self._snapshot = snapshot.build(self.cfg, refresh=refresh)
            self._at = time.monotonic()
            return self._snapshot


def create_app(cfg: Config | None = None) -> FastAPI:
    cfg = cfg or Config.from_env()
    app = FastAPI(title="btc_dashboard")
    cache = SnapshotCache(cfg)
    state = {"answer": None, "last_ask": 0.0}

    def render(refresh: int | None = PAGE_REFRESH) -> str:
        return page.render_html(
            cache.get(), ask=True, answer=state["answer"], refresh=refresh
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render()

    @app.post("/refresh")
    def force_refresh() -> RedirectResponse:
        cache.get(refresh=True)
        return RedirectResponse("/", status_code=303)

    @app.post("/ask")
    def ask(q: str = Form("")) -> RedirectResponse:
        question = (q or "").strip()
        now = time.monotonic()

        if not question:
            state["answer"] = None
        elif now - state["last_ask"] < ASK_COOLDOWN:
            state["answer"] = {
                "question": question,
                "error": f"asked less than {ASK_COOLDOWN:.0f}s ago — "
                         f"each question costs money, so this one was not sent",
            }
        else:
            state["last_ask"] = now
            result = analyst.ask(cache.get(), question, cfg)
            state["answer"] = {
                "question": question,
                "text": result.text,
                "error": result.error,
                "provider": result.provider,
                "model": result.model,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
            }

        # Redirect rather than render: a rendered POST response would re-submit
        # on reload, and the page's meta-refresh would re-issue it every minute.
        return RedirectResponse("/", status_code=303)

    return app


app = None  # populated by main(); uvicorn users should call create_app()


def _port_free(host: str, port: int) -> bool:
    """Whether the port can be bound, so the failure can explain itself."""
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host if host != "localhost" else "127.0.0.1", port))
        except OSError:
            return False
    return True


def main(argv=None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="btc-dashboard-web",
        description="Serve the dashboard locally, with the analyst box.",
    )
    # Default 127.0.0.1 and not a documented 0.0.0.0 example, because this
    # process can spend money and the example is what gets copied.
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default 127.0.0.1 — loopback only)")
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = p.parse_args(argv)

    try:
        import uvicorn
    except ImportError:
        print('uvicorn is not installed — pip install -e ".[web]"')
        return 1

    if not _port_free(args.host, args.port):
        # uvicorn reports this as a bare errno, which does not say what to do
        # about it. On a host already running another local dashboard, a port
        # clash is the most likely first-run failure.
        print(
            f"port {args.port} on {args.host} is already in use — something else "
            f"is listening there (bitcoin_peer_monitor uses 8000 by default).\n"
            f"    btc-dashboard-web --port {args.port + 1}"
        )
        return 1

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print(
            f"WARNING: binding {args.host} exposes this beyond loopback. This "
            f"process holds your LLM provider key and /ask spends money. Prefer "
            f"the default and reach it over an SSH tunnel:\n"
            f"    ssh -L {args.port}:localhost:{args.port} <host>"
        )

    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
