# Proxy Rotator Manager

Local app to **test, manage, and rotate residential/datacenter proxies** behind a single localhost gateway.

Point your browser or any HTTP/SOCKS-aware app at `127.0.0.1:8888` → the gateway forwards each request through a healthy proxy from your pool, rotates automatically, and quarantines proxies that get burned (403 / 429 / CAPTCHA / timeout).

Stack: **Python 3.11+ · FastAPI · aiohttp · SQLite · HTMX**. Cross-platform (macOS + Windows). Desktop (Electron) version planned for v2.

---

## Quick start

```bash
# 1. Clone / unzip into a folder
cd proxy-rotator

# 2. Create venv + install
python3 -m venv .venv
source .venv/bin/activate      # macOS / Linux
# .venv\Scripts\activate       # Windows PowerShell
pip install -r requirements.txt

# 3. Copy env template
cp .env.example .env           # macOS / Linux
# copy .env.example .env       # Windows

# 4. Run
./scripts/start.sh             # macOS
# scripts\start.bat            # Windows
```

Then:

- **Admin UI**     → <http://127.0.0.1:8080>
- **Proxy gateway** → `127.0.0.1:8888` (HTTP) · `127.0.0.1:1080` (SOCKS5)

Paste your proxy list in the UI, click **Test all**, enable rotation, point your browser to the gateway.

Browser setup: see [`docs/BROWSER_SETUP.md`](docs/BROWSER_SETUP.md).

---

## Features (MVP — v1)

- [x] Import proxies by paste (format `user:pass@host:port`)
- [x] Parse + dedup + persist in SQLite
- [x] Async health check (parallel, GET against `https://api.ipify.org`)
- [x] Dashboard: host, port, status, latency, score, success %, last check
- [x] Buttons: test one / test all / remove / clear dead
- [x] Local HTTP + SOCKS5 gateway with pluggable rotation strategies
- [x] Burn detector (status + content + timeout + latency spike)
- [x] Health score + auto-quarantine (10 min) + re-test
- [x] Credentials encrypted at rest (Fernet with PBKDF2-derived key)
- [x] REST API + WebSocket for live stats

## Dashboard

Open <http://127.0.0.1:8080> after startup. The interface uses server-rendered
HTML, locally bundled HTMX and WebSocket updates. Illustrative data below;
the bound addresses shown in this sketch assume the default listeners started.
The Gateway panel displays actual bound addresses, live counters and connection
test buttons for both HTTP and SOCKS5.

```text
+--------------------------------------------------------------------------------+
| PROXY ROTATOR                                         Live connection: online  |
+--------------------------------------------------------------------------------+
| Total: 50 | Alive: 46 | Quarantine: 2 | Dead: 2 | Avg latency: 312 ms             |
+--------------------------------------------------------------------------------+
| Pool                         [Test all] [Clear dead]                            |
| Host:port       Status       Latency  Score  Success%  Last check   Actions     |
| 203.0.113.10:80 alive          240 ms   94.2    98%     10:42:01    Test Remove |
| 203.0.113.11:80 quarantine     890 ms   18.4    62%     10:41:58    Test Remove |
+--------------------------------------------------------------------------------+
| Import                         | Rotation               | Gateway              |
| [Paste proxy list            ] | [round_robin         v] | HTTP bound:          |
| [One proxy per line          ] | [Apply]                | 127.0.0.1:8888       |
| [Import and check]             | random / weighted /    | SOCKS5 bound:        |
| 2,000 lines; bad lines skipped | sticky also available  | 127.0.0.1:1080       |
+--------------------------------------------------------------------------------+
```

The controls stack vertically on narrow screens; the proxy table scrolls
horizontally. Empty pools, connection loss and import errors have explicit
feedback. Credentials are not displayed in the proxy table.

## Planned (v2)

- Electron desktop app (same backend, native shell)
- Multi-pool (regional pools)
- Sticky session per domain
- Browser-config one-click (Firefox preset, SwitchyOmega export)
- Dashboard graph (requests/min, success rate over time)

---

## Project layout

```
proxy-rotator/
├── README.md                   This file
├── CLAUDE.md                   Instructions for Codex / Claude agent
├── SPEC.md                     Full technical spec
├── ARCHITECTURE.md             Layers + data flow
├── TASKS.md                    Prioritised todo list for the agent
├── docs/
│   ├── API.md                  REST + WebSocket endpoints
│   ├── BROWSER_SETUP.md        Firefox / Chrome / system setup
│   ├── SECURITY.md             Credentials, DNS leak, hardening
│   └── MIGRATION_ELECTRON.md   Plan for v2 desktop wrapper
├── app/
│   ├── main.py                 FastAPI entry
│   ├── config.py               Settings (pydantic-settings)
│   ├── db.py                   SQLite async init
│   ├── models.py               Pydantic DTOs
│   ├── crypto.py               Fernet encrypt/decrypt
│   ├── parser.py               Parse user:pass@host:port
│   ├── health_checker.py       Async proxy testing
│   ├── pool.py                 Pool state + scoring
│   ├── rotator.py              Rotation strategies
│   ├── gateway_http.py         HTTP CONNECT + forward proxy
│   ├── gateway_socks.py        SOCKS5 server
│   ├── burn_detector.py        Rules to detect burned proxy
│   ├── routes/
│   │   ├── proxies.py
│   │   ├── pool.py
│   │   └── gateway.py
│   └── ui/
│       ├── templates/dashboard.html
│       └── static/{app.js, styles.css}
├── scripts/
│   ├── start.sh                macOS launcher
│   ├── start.bat               Windows launcher
│   └── install.sh              Dev install
├── tests/
│   ├── test_parser.py
│   ├── test_health_checker.py
│   ├── test_rotator.py
│   └── test_burn_detector.py
├── pyproject.toml
├── requirements.txt
├── .env.example
└── .gitignore
```

---

## Legal

Use only for proxies you have the right to use. Respect source ToS and target site ToS. The author is not responsible for abuse.


## Smoke test

`./scripts/smoke_gateway.sh` verifies the **real local HTTP gateway**, including
HTTPS CONNECT, by requesting `https://api.ipify.org` three times. It succeeds
only when at least two distinct exit IPs are observed. It never falls back to
a direct request and overrides NO_PROXY so the gateway cannot be bypassed.

Prerequisites (macOS/Linux): Python 3.11+, curl, an existing `.venv`, installed
dependencies and a `.env` with the correct `MASTER_SECRET` for the stored salt
and database. A master secret alone does not provide proxies: first import at
least two working upstreams with distinct exit IPs using the UI. Prefer
`ROTATION_STRATEGY=round_robin`; weighted/random/sticky selection can legitimately
return the same exit three times. Stop any running app before the test.

The script starts `scripts/start.sh`, waits up to 30 seconds for admin readiness
on `127.0.0.1:8080`, then up to 60 seconds for at least two alive proxies. Each
IP request through `127.0.0.1:8888` has a 10-second timeout. Use those default
ports in `.env`. Startup/dependency installation must fit the readiness timeout;
run `start.sh` once beforehand on a fresh installation.

Logs go to `/tmp/proxy-rotator-smoke.log` (owner-only). Only validated IPs and
fixed status messages are printed. The script sends SIGTERM to its own server
process group, waits up to five seconds, then uses SIGKILL if needed. Exit 0
means the rotation proof passed; exit 1 means failure or missing prerequisites.
This live smoke is deliberately **not run in CI**; automated tests use fake
local upstreams and isolated script harnesses instead.

### Live proxy usage

The top panels show active connections and recent completed connections, the
selected upstream, last verified exit IP, country (when known), health score and
elapsed connection time. The exit IP is the latest health-probe observation;
a provider may change it per request. The score is not bandwidth or a prediction
of remaining lifetime. Changes of observed exit IP appear after another probe.

Country lookup is opt-in (`GEOLOCATION_ENABLED=true`): the configured provider
receives exit IPs, never credentials. Without it, country remains unknown.
Country/score/pinned-only filters affect new connections and reset on restart.

Each gateway has a **Tester la connexion** button. It makes a real IP request
through that listener, reports the exit IP and elapsed time, and stops after
10 seconds. At least one eligible working upstream is required. This also
exercises rotation; it does not merely check whether the port is open.

Rotation **Toutes les X minutes** keeps the same eligible proxy for the selected
interval (1–1440 minutes). The next connection after expiry selects the next
proxy. Existing tunnels continue. An unavailable proxy is replaced immediately;
with only one eligible proxy, a different IP cannot be guaranteed.
