# docs/BROWSER_SETUP.md

How to send browser or system traffic through the local gateway.

Default gateway bind : `127.0.0.1:8888` (HTTP) and `127.0.0.1:1080` (SOCKS5).

---

## Firefox (recommended — isolated from system)

1. `about:preferences` → **Network Settings** → **Manual proxy configuration**.
2. HTTP proxy : `127.0.0.1`, port `8888`, check **Also use this proxy for HTTPS**.
3. Or SOCKS : host `127.0.0.1`, port `1080`, SOCKS v5.
4. **`about:config`** → set `network.proxy.socks_remote_dns = true` (prevents DNS leak).
5. Test with <https://api.ipify.org>. The IP shown must be one of your proxies, not your real IP.

---

## Chrome / Edge / Brave

System proxy (OS-level). Easier: install the **Proxy SwitchyOmega** extension.

1. New profile → **Manual**.
2. Protocol HTTP, host `127.0.0.1`, port `8888`.
3. Enable the profile. Chrome will route via the gateway.

For DNS leak protection, use SOCKS5 (port `1080`) with "DNS over proxy" enabled in the extension.

---

## macOS system-wide

System Settings → Network → *selected interface* → Details → Proxies :

- Web Proxy (HTTP) : `127.0.0.1 : 8888`
- Secure Web Proxy (HTTPS) : `127.0.0.1 : 8888`
- SOCKS Proxy : `127.0.0.1 : 1080`

Click **OK** → **Apply**. Reverts to direct when toggled off.

CLI test : `curl -x http://127.0.0.1:8888 https://api.ipify.org`.

---

## Windows 10 / 11 system-wide

Settings → Network & Internet → Proxy → **Manual proxy setup**.

- Address : `127.0.0.1`
- Port : `8888`
- Save.

For SOCKS, no built-in support — use **Proxifier** or **Proxycap** (paid), or stay with HTTP CONNECT.

CLI test (PowerShell) : `curl.exe -x http://127.0.0.1:8888 https://api.ipify.org`.

---

## cURL

```bash
curl -x http://127.0.0.1:8888 https://api.ipify.org          # HTTP proxy
curl --socks5-hostname 127.0.0.1:1080 https://api.ipify.org  # SOCKS5 with remote DNS
```

Run three times → should see three different IPs if `weighted` or `round_robin` strategy is active.

---

## Troubleshooting

| Symptom                      | Likely cause                                | Fix                                           |
|------------------------------|---------------------------------------------|-----------------------------------------------|
| `503 Pool Empty`             | No proxy has `alive` status                 | Click **Test all** in UI, wait, re-check      |
| Same IP every request        | Browser caches connection                   | Open private window; or restart browser       |
| DNS resolves to local ISP    | SOCKS DNS leak                              | Set `network.proxy.socks_remote_dns = true`   |
| `502 Bad Gateway` sometimes  | Burn detector retried N times and gave up   | Normal under heavy burn — more proxies needed |
| Connection refused           | Gateway not running                         | Check logs — port might be bound by another process |
