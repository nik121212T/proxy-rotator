# docs/MIGRATION_ELECTRON.md

What changes when we wrap v1 in Electron for v2. Not a v1 deliverable — but write the backend so these changes stay cheap.

---

## Why Electron

- Single `.dmg` / `.exe` the user double-clicks. No terminal.
- OS keychain for `MASTER_SECRET`.
- Native menu + tray icon.
- Can intercept proxy config at the OS level if desired (via helper).

## Why not pure Electron (JS backend)

- Our rotation + probing is cleaner in asyncio Python.
- aiohttp_socks + SOCKS5 implementations in Python are more mature than npm equivalents.

## Verdict

Keep Python backend. Electron becomes a **thin shell** that spawns Uvicorn as a child process and renders the dashboard in a `BrowserWindow`.

---

## Design

```
┌──────────────────────────────────────────┐
│                Electron main              │
│  - spawn python/uvicorn child             │
│  - keep stdin for IPC                     │
│  - os-keychain for MASTER_SECRET          │
│  - BrowserWindow(http://127.0.0.1:8080)   │
└───────────────┬──────────────────────────┘
                │ child_process
                ▼
         Python backend (same v1)
```

---

## What the backend needs to be ready for

| Change                                     | How to prepare in v1                                                     |
|--------------------------------------------|--------------------------------------------------------------------------|
| `MASTER_SECRET` from OS keychain, not env  | `config.py` already reads from env → keep it; wrapper sets env before spawn. |
| Random port assignment                     | Accept `ADMIN_PORT=0` → log the actual bound port on stdout.             |
| Clean shutdown on SIGTERM                   | Use `lifespan` to close DB and cancel tasks.                             |
| Dashboard assets served self-contained      | No CDN links. Vendor HTMX + CSS locally.                                 |
| Health check target configurable            | Already `HEALTH_PROBE_URL` env var.                                      |
| User's data folder outside app bundle       | Allow absolute `DB_PATH` from env.                                       |

---

## Packaging path (v2 sketch)

1. `pyinstaller --onefile app/main.py` → `backend` binary.
2. `electron-builder` bundles the binary into the installer.
3. On first run, Electron asks for `MASTER_SECRET`, stores in keychain, spawns backend.
4. Backend writes to `~/Library/Application Support/ProxyRotator/` (macOS) or `%APPDATA%\ProxyRotator\` (Windows).

Don't start this before v1 ships green.
