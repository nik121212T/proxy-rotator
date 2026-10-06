# docs/SECURITY.md

Threat model + hardening checklist. Read before deploying anywhere beyond your own machine.

---

## In scope

- **Localhost deployment** (default). Only the current user can talk to the UI and gateways.
- Proxy credentials at rest.
- Protection against accidental leaks (DNS, direct connections when pool empty).

## Out of scope (v1)

- Hardening for public exposure. Don't open the admin port to the internet.
- Hiding your identity from the sites you visit — proxies forward your traffic; they don't anonymise it beyond IP.
- Protection from a malicious proxy provider — proxies can MITM any non-TLS traffic.

---

## Threats

| Threat                                      | Mitigation                                                                       |
|---------------------------------------------|----------------------------------------------------------------------------------|
| Credentials dumped from SQLite              | `password_enc` is Fernet-encrypted with a key derived from `MASTER_SECRET`.      |
| Credentials in logs                         | Mask passwords in every logger (`user:***`). Enforced by lint rule.              |
| Direct-connect leak when pool is empty      | Gateway returns `503 Pool Empty`. Never bypass.                                  |
| DNS leak when using SOCKS5                  | Resolve through the upstream (ATYP=0x03 passthrough). Document Firefox toggle.   |
| Admin UI accessed from LAN                  | Default `BIND_HOST=127.0.0.1`. Changing to `0.0.0.0` prints a startup warning.   |
| Replay of a burned proxy                    | Quarantine 10 min + score decay. Re-test before re-admit.                        |
| Pool poisoning via subscription URL         | Parse strictly, dedup, cap import size at `MAX_IMPORT_LINES` (default 2000).     |
| Config injection via env                    | `pydantic-settings` validates types + ranges on startup.                          |
| Dependency CVE                              | `pip-audit` in CI. Weekly Renovate PRs for updates.                              |

---

## Secrets handling

- `MASTER_SECRET` is **not** stored. User supplies it via `.env` (git-ignored) or env var.
- The derivation salt lives in `data/.salt` (0600). First launch generates one if missing.
- Loss of `MASTER_SECRET` ≡ loss of password_enc. There is no recovery. The app logs this on startup and refuses to continue decryption.
- No telemetry. No crash reports leave the machine.

---

## Hardening checklist for v1

- [ ] All logger calls go through `app.log.mask()` which redacts user:pass tokens.
- [ ] `grep -R 'password' app/ | grep -v password_enc` returns nothing.
- [ ] `pytest tests/security/` covers : empty pool → 503 ; wrong key → EX_CONFIG ; LAN bind warning ; parser rejects malformed lines.
- [ ] CI runs `pip-audit` and fails on HIGH/CRITICAL.
- [ ] `scripts/start.sh` refuses to start if `MASTER_SECRET` is a known weak value (`password`, `changeme`, etc.) outside dev mode.

---

## When you ship v2 (Electron)

- Put the HTTP gateway, SOCKS gateway, and admin UI behind the Electron sandbox.
- Store `MASTER_SECRET` in the OS keychain (`keytar` on Electron side).
- Signed builds + notarised on macOS.
- Code-sign on Windows.
