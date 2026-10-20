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
| Credentials in logs                         | `SecretStr`/`SecretBytes` masks, field whitelists and the global 422 handler; asserted by tests, **not** by a lint rule. |
| Direct-connect leak when pool is empty      | Gateway returns `503 Pool Empty`. Never bypass.                                  |
| DNS leak when using SOCKS5                  | Resolve through the upstream (ATYP=0x03 passthrough). Document Firefox toggle.   |
| Admin UI reached from a hostile page        | `HostGuard` refuses unknown `Host` values (DNS rebinding); `CrossSiteGuard` refuses state-changing requests a browser marks cross-site. |
| Admin UI accessed from LAN                  | `BIND_HOST=127.0.0.1` by default; a non-loopback bind is **refused** unless `ALLOW_REMOTE_BIND`, `ADMIN_TOKEN` and `ADMIN_ALLOWED_HOSTS` are all set. |
| Replay of a burned proxy                    | Quarantine 10 min + score decay. Re-test before re-admit.                        |
| Retried request after a partial response    | The gateway stops retrying once a response byte has been committed (RFC 9112 §9.2). |
| Request smuggling (CL.TE)                   | Ambiguous or conflicting framing is rejected with 400 (RFC 9112 §6.1/§6.3).      |
| Replay of a non-idempotent request          | POST/PATCH take a single attempt (RFC 9110 §9.2.2); the origin's status is passed through. |
| Pool poisoning via subscription URL         | Parse strictly, dedup, cap import size at `MAX_IMPORT_LINES` (default 2000).     |
| Config injection via env                    | `pydantic-settings` validates types + ranges on startup.                          |
| Dependency CVE                              | `pip-audit -r requirements.txt` runs as a blocking CI job. No automated updater is configured. |

---

## Exposure guards

Three middlewares in `app/security.py` wrap every HTTP request:

1. **`HostGuard`** — only the names in `configured_hosts()` are answered:
   loopback aliases, the configured `BIND_HOST`, `ADMIN_ALLOWED_HOSTS`, and the
   names used by ASGI test clients. A page served from an attacker domain that
   resolves to `127.0.0.1` reaches the socket but sends that domain in `Host`,
   so it is refused with 400.
2. **`CrossSiteGuard`** — `POST`/`PUT`/`PATCH`/`DELETE` carrying
   `Sec-Fetch-Site: cross-site`, a foreign `Origin`, or `Origin: null` are
   refused with 403. Without this, any visited page could POST to
   `127.0.0.1:8080` and clear the pool or spend provider credits.
3. **`TokenGuard`** — a no-op when `ADMIN_TOKEN` is unset. When set, every
   route except `/healthz`, `/login` and `/static/*` requires the token, as
   `Authorization: Bearer …` or through the cookie set by the `/login` form.
   Comparisons use `secrets.compare_digest`.

A non-loopback `BIND_HOST` requires `ALLOW_REMOTE_BIND=true`, a non-empty
`ADMIN_TOKEN` and a non-empty `ADMIN_ALLOWED_HOSTS`; otherwise startup is
refused with `EX_CONFIG` (78). The gateways themselves are **not** covered by
these guards — they speak CONNECT, SOCKS5 and raw bytes — so a remote bind
publishes two unauthenticated proxies. The startup warning says so explicitly. Set `GATEWAY_BIND_HOST=127.0.0.1`
to keep both proxy gateways local while sharing only the admin UI. Host,
origin and token guards also protect WebSocket event subscriptions.

### Known residual risks

- **`/api/proxies/export` returns plaintext credentials** to any caller allowed
  past the guards. On the default loopback setup that means any local process
  (and any local user, on a shared machine). It is the documented export
  feature; protect it by setting `ADMIN_TOKEN`, or do not leave the port
  reachable by untrusted local accounts.
- **The token travels in clear text** over plain HTTP in remote mode. Use a
  trusted network or terminate TLS in front of the admin port.
- **The gateways have no authentication** in any mode: anything that can reach
  ports 8888/1080 can spend your proxies.
- **Database files are mode 0644** while `data/.salt` is 0600. Credentials are
  encrypted, so this is defence-in-depth only.


---

## Secrets handling

- `MASTER_SECRET` is **not** stored. User supplies it via `.env` (git-ignored) or env var.
- The derivation salt lives in `data/.salt` (0600). First launch generates one if missing.
  The salt is 16 random bytes, created exclusively and reused on later starts.
  A malformed existing salt is rejected, never silently replaced. POSIX modes
  are enforced on macOS/Linux; Windows uses the directory's inherited ACLs.
- Loss of `MASTER_SECRET` ≡ loss of password_enc. There is no recovery. The app logs this on startup and refuses to continue decryption.
- No telemetry. No crash reports leave the machine.

---

## Hardening checklist for v1

- [x] Credentials cannot reach a logger, a JSON response or the WebSocket
      stream by construction: `password_enc` is `SecretBytes` with
      `exclude=True`, `ParsedProxy.__str__` masks as `user:***`, and
      `tests/test_proxy_api.py`, `tests/test_websocket.py`, `tests/test_models.py`
      and `tests/test_crypto.py` assert it. There is **no** masking lint rule and
      no `app/log.py`; this is verified by tests, not by static analysis.
- [ ] `grep -R 'password' app/ --include='*.py' | grep -v password_enc` still
      returns legitimate mentions (parser, decryption, SOCKS credentials). This
      item was previously written as if the command returned nothing; it never
      did. Tracked in AUDIT.md as C4.
- [x] `tests/test_security.py` covers host validation, cross-site refusal and the
      admin token; `tests/test_main.py` covers the remote-bind refusal and the
      LAN warning; `tests/test_crypto.py` covers the wrong key (EX_CONFIG);
      `tests/test_parser.py` covers malformed lines.
- [x] CI runs `pip-audit -r requirements.txt` as a blocking job (see the
      `audit` job in `.github/workflows/ci.yml`). No Renovate configuration.
- [ ] `scripts/start.sh` refuses only a blank `MASTER_SECRET` or the literal
      `CHANGE_ME_NOW`, via the `Settings` validator. There is no weak-secret
      blacklist (`password`, `changeme`, …); that claim was inaccurate.

---

## When you ship v2 (Electron)

- Put the HTTP gateway, SOCKS gateway, and admin UI behind the Electron sandbox.
- Store `MASTER_SECRET` in the OS keychain (`keytar` on Electron side).
- Signed builds + notarised on macOS.
- Code-sign on Windows.
