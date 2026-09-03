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
- a request another page caused is refused, and so is a `Host` that is not
  ours. Binding loopback keeps other *machines* out; it does not keep other
  *pages* out, because the browser that would be tricked into making the
  request is already inside — and a form POST needs no permission from us to
  arrive;
- `/ask` has a cooldown, because each request costs real money and a
  double-submit should not cost twice;
- the token count of every answer is shown, so the cost is visible rather than
  invisible.

Collection is decoupled from HTTP entirely. The page holds a snapshot in
memory with a short TTL, so an auto-refreshing tab does not scrape Farside or
poll CoinGecko once a minute, and asking three questions costs three LLM calls
and zero collections.

The page updates its data regions in place from `/live` rather than reloading
itself. A reload would discard a half-typed question, and a tick that lands
mid-sentence is exactly when that hurts most. The ask box therefore changes
only when an answer comes back from a POST.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlsplit

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

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
# Where the page re-reads its data regions from. The page updates those in
# place rather than reloading, so the ask box keeps whatever is typed in it
# while the numbers move underneath.
LIVE_PATH = "/live"
# Not 8000: bitcoin_peer_monitor conventionally takes that, and two local
# dashboards on one host should not fight over a port by default.
DEFAULT_PORT = 8001

# Names this service answers to when it is bound to loopback. Compared
# without the port on purpose: an SSH tunnel may forward a different local
# port than the one bound here (`ssh -L 9001:localhost:8001`), and a port is
# not what a rebinding attack controls. The name is.
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1"})

# Verbs with no side effect, which therefore need no cross-origin check. A
# cross-origin GET cannot be read back by the page that caused it, and none
# of these spends money.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _authority(value: str | None) -> tuple[str, int | None] | None:
    """`(hostname, port)` from a `Host` header or an `Origin` URL.

    Parsed rather than split on ":", because an IPv6 literal carries colons of
    its own — `[::1]:8001` is one host and one port, not five. A `Host` header
    has no scheme, so `//` is prepended to make it parse as an authority; an
    `Origin` already carries one and is left alone.
    """
    if not value:
        return None
    try:
        parts = urlsplit(value if "//" in value else f"//{value}")
        return (parts.hostname or "", parts.port)
    except ValueError:
        return None


def _caused_by_another_site(request: Request) -> bool:
    """Whether some other page made the browser send this request.

    A form POST is a "simple request": no preflight, and the browser sends it
    whatever the target says about CORS. The attacker cannot read the reply,
    but the side effect has already happened — and here the side effect spends
    money, runs SQL, and writes an answer into the page the operator will read
    next. So it has to be refused before the handler, not after.

    `Sec-Fetch-Site` is the answer when it is there. Note the test is against
    `same-origin` and not merely `cross-site`: a page on another *port* of
    localhost reports `same-site`, and that is still someone else's page.

    `Origin` is the fallback for a browser too old to send the first, compared
    on host and port because that is what an origin is. Neither header present
    means the caller is not a browser — curl, a script — and a request nobody's
    browser was tricked into making is not a forgery.
    """
    site = request.headers.get("sec-fetch-site")
    if site is not None:
        return site != "same-origin"
    origin = request.headers.get("origin")
    if origin is None:
        return False
    return _authority(origin) != _authority(request.headers.get("host"))


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


def create_app(cfg: Config | None = None, *,
               require_local_host: bool = True) -> FastAPI:
    """The app. `require_local_host` should stay on unless the bind is wider.

    Binding loopback keeps other machines out; it does not keep other *pages*
    out, because the browser making the request is already inside. That is what
    the middleware below is for.
    """
    cfg = cfg or Config.from_env()
    app = FastAPI(title="btc_dashboard")
    cache = SnapshotCache(cfg)
    state = {"answer": None, "last_ask": 0.0}

    @app.middleware("http")
    async def refuse_requests_we_did_not_cause(request: Request, call_next):
        """Two guards, and they close different holes.

        The `Host` check is against **DNS rebinding**, which is the one attack
        an origin check cannot see: the attacker points a name they own at
        127.0.0.1, and their page then *is* same-origin — `Origin` matches
        `Host`, `Sec-Fetch-Site` says `same-origin`, and every check below
        passes. What gives it away is the name in `Host`, which is theirs and
        not one of ours. Only meaningful while the bind is loopback, hence the
        flag: on a wider bind the legitimate name is whatever the operator's
        network calls this machine, and we do not know it.

        The cross-origin check is against ordinary CSRF, and applies only to
        the verbs that change something.

        Refused before the handler runs, which is the point: `state["answer"]`
        is never written and `ASK_COOLDOWN` is never consumed, so a forged
        request cannot plant an answer on the page or lock out a real question
        for the next five seconds.

        Middleware rather than a per-route dependency so that a route added
        later inherits this instead of needing someone to remember it.
        """
        host = _authority(request.headers.get("host"))
        if require_local_host and (host is None or host[0] not in LOCAL_HOSTNAMES):
            return PlainTextResponse(
                "This service answers on loopback only. Reach it at "
                "http://localhost:<port>, over an SSH tunnel if it is remote.",
                status_code=403,
            )
        if request.method not in SAFE_METHODS and _caused_by_another_site(request):
            return PlainTextResponse(
                "Refused: this request came from another page. Ask from the "
                "dashboard itself — each question spends real money.",
                status_code=403,
            )
        return await call_next(request)

    def render(refresh: int | None = PAGE_REFRESH) -> str:
        return page.render_html(
            cache.get(), ask=True, answer=state["answer"], refresh=refresh,
            live_endpoint=LIVE_PATH,
        )

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return render()

    @app.get(LIVE_PATH, response_class=HTMLResponse)
    def live() -> str:
        """The data regions alone, for the page's in-place update.

        Deliberately without the ask box: this response is what a tick writes
        into an open page, and it must not be able to replace a field someone
        is typing into. It also serves the shared in-memory snapshot, so a tab
        polling every minute collects nothing.
        """
        return page.render_live(cache.get())

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
                # Carried through so the page can show what was run. An answer
                # whose figures came from queries the reader cannot see is not
                # checkable, and checkable is the point.
                "tool_calls": result.tool_calls,
                "no_tools_reason": result.no_tools_reason,
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

    local = args.host in LOCAL_HOSTNAMES
    if not local:
        print(
            f"WARNING: binding {args.host} exposes this beyond loopback. This "
            f"process holds your LLM provider key and /ask spends money. Prefer "
            f"the default and reach it over an SSH tunnel:\n"
            f"    ssh -L {args.port}:localhost:{args.port} <host>\n"
            f"The Host check is off on a wider bind — the legitimate name is "
            f"whatever your network calls this machine, which this process "
            f"cannot know. Cross-origin POSTs are still refused."
        )

    uvicorn.run(create_app(require_local_host=local),
                host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
