# deploy

## `btc-dashboard-web.service`

Keeps the web dashboard running. Copy to `/etc/systemd/system/`, adjust
`User`/`Group` and the `/home/mikeoc` paths, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now btc-dashboard-web
journalctl -u btc-dashboard-web -f
```

Three differences from a plain uvicorn unit, each deliberate:

**It binds `127.0.0.1`, not `0.0.0.0`.** This process holds your LLM provider
key and `/ask` spends money per request, so a LAN-reachable bind lets anyone who
can reach the port spend your budget. Reach it from elsewhere with a tunnel:

```bash
ssh -L 8001:localhost:8001 <host>
```

**It runs the console script, not `uvicorn` directly.** `btc-dashboard-web`
carries the safe bind default, the port pre-flight check, and the wider-bind
warning. Invoking uvicorn straight from `ExecStart` bypasses all three.

**The API key is not in the unit.** `systemctl show` prints a unit's full
environment, so a key there is readable by any local user. It is read from
`~/.config/btc_dashboard/env` (chmod 600) instead — which also means one file
serves the CLI, cron, and this service.

`HOME` is set explicitly because systemd starts with a minimal environment and
it is what locates the warehouse, the cache, and that env file.

### Port

8001 by default, since `bitcoin_peer_monitor` conventionally holds 8000. To run
both, tunnel both:

```bash
ssh -L 8000:localhost:8000 -L 8001:localhost:8001 <host>
```
