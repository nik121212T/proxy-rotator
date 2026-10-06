# CLAUDE.md — Agent instructions

You are the implementation agent (Codex / Claude) for **Proxy Rotator Manager**.

Read this file **first**, then `SPEC.md`, then `TASKS.md`. Everything else is reference.

---

## Mission

Build an MVP that lets the user:

1. Paste a list of proxies in format `user:pass@host:port`.
2. Test them in parallel, see live health in a dashboard.
3. Point any browser / app at `127.0.0.1:8888` (HTTP) or `127.0.0.1:1080` (SOCKS5) and have every outbound request go through a working proxy from the pool, rotated automatically.
4. Have burned proxies (403 / 429 / CAPTCHA / timeout) auto-removed or quarantined without manual action.

Target OS: **macOS + Windows**. Keep the stack stdlib-friendly to stay simple.

---

## Non-negotiable rules

1. **Never log raw credentials** (`user:pass`). Mask as `user:***`. Enforce in every logger and every error message.
2. **Encrypt credentials at rest**. Use `cryptography.Fernet` with a key derived from `MASTER_SECRET` (env). Never store plaintext in SQLite.
3. **Localhost only by default**. Gateway + admin UI bind to `127.0.0.1`. Expose on LAN only if the user flips `BIND_HOST=0.0.0.0` in `.env` — print a loud warning when they do.
4. **No external telemetry**. No analytics, no auto-updater pinging a server. Only outbound traffic = health checks + user requests through gateway.
5. **DNS leak protection**. For SOCKS5, resolve DNS through the proxy. Document the Firefox `network.proxy.socks_remote_dns = true` requirement.
6. **Fail closed**. If the pool has zero healthy proxies, the gateway returns `503 Pool Empty` instead of connecting directly. Never silently bypass.
7. **Deterministic parser**. Must round-trip: `parse(serialize(parse(x))) == parse(x)`. Add a property-based test for this.
8. **No scope creep**. If a feature is not in `TASKS.md`, open a GitHub issue instead of implementing it.

---

## How to work

1. **Pick the next unchecked task** in `TASKS.md`. Follow the order unless blocked.
2. **Write the test first** (TDD). Place it under `tests/`. Run `pytest -k <test_name>` and watch it fail.
3. **Implement the smallest change** that makes the test pass.
4. **Refactor** only after green.
5. **Update `TASKS.md`** — check the box, add a one-line note if you deviated from the plan.
6. **Commit** per task with a conventional message:
   `feat(pool): add health score decay`, `test(parser): round-trip property`, `fix(gateway): CONNECT leak`.

### When stuck

- Re-read `SPEC.md` for the component you are touching.
- Re-read `ARCHITECTURE.md` for the layer boundaries.
- If still unclear, **ask the user in a terminal prompt** — do not guess silently.

### When finishing a task

Run before committing:

```bash
pytest -q
ruff check app tests
mypy app
```

All three green → commit.

---

## Style

- **Python**: PEP 8, type hints everywhere, `pydantic` for DTOs, `pathlib.Path` not `os.path`.
- **Async**: everything in the hot path is `async def`. Blocking calls go in a `run_in_executor`.
- **Logging**: one `logger = logging.getLogger(__name__)` per module. Format with structured key=value, not f-string junk.
- **Error handling**: `try/except` only when you can act on it. Re-raise or wrap in a domain exception; don't swallow.
- **No print()** in production code — use the logger.

---

## Deliverables for v1 (MVP)

- Runs on macOS + Windows with one `start.sh` / `start.bat`.
- Dashboard at `http://127.0.0.1:8080` lists proxies with live status (HTMX, no SPA).
- Gateway at `127.0.0.1:8888` forwards HTTP CONNECT.
- Gateway at `127.0.0.1:1080` forwards SOCKS5.
- Health checker reports status + latency + score within 10 s of import for a pool of 50 proxies.
- Burn detector moves a proxy to `quarantine` after 3 consecutive fails. Auto-retests after 10 min.
- Full test suite green on CI (GitHub Actions matrix: `ubuntu`, `macos`, `windows`).

Non-goals for v1 (defer to v2): Electron shell, multi-pool, graphs, authenticated UI.

---

## Prior art to reuse (don't reinvent)

- **proxybroker2** (<https://github.com/bluet/proxybroker2>) — Python async proxy finder/checker/server. We don't need the *finder* (we already have paid credentials), but **steal patterns** from its:
  - `ProxyChecker` (judge rotation, latency measurement)
  - `Server` (local SOCKS/HTTP forward proxy)
  - `Pool` (health-weighted selection)

  You can either vendor relevant modules or re-implement smaller focused versions. Our scope is narrower (private pool, no discovery), so a slim reimplementation is usually cleaner than carrying the whole lib. **Decide per module and justify in the commit message.**

- **mitmproxy** — only if we need HTTPS MITM for burn detection on response body. Default: no MITM; detect burn from status codes + timeouts.

- **aiohttp-socks** — SOCKS5 client used in health checker + HTTP gateway upstream.

---

## When done

Open a PR with:

1. Changelog of what shipped vs `TASKS.md`.
2. Screenshot (or text sketch) of the dashboard.
3. Example output of `curl -x http://127.0.0.1:8888 https://api.ipify.org` run three times showing three different exit IPs.
4. A line saying which tasks remain in `TASKS.md`.

---

## Reference docs

| File                              | What's in it                                                       |
|-----------------------------------|--------------------------------------------------------------------|
| `SPEC.md`                         | Data model, scoring formula, burn rules, parser grammar            |
| `ARCHITECTURE.md`                 | Component diagram, data flow, concurrency model                    |
| `TASKS.md`                        | Prioritised, checkable todo list for v1                            |
| `docs/API.md`                     | Every REST endpoint + the one WebSocket channel                    |
| `docs/BROWSER_SETUP.md`           | Firefox / Chrome / macOS / Windows system proxy configuration      |
| `docs/SECURITY.md`                | Threat model + hardening checklist                                 |
| `docs/MIGRATION_ELECTRON.md`      | What has to change for the v2 desktop wrapper (so keep it in mind) |

Read in that order on first run.
