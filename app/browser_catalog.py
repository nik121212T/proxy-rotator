"""Local browser discovery and native launch configuration; never personal profiles."""

import json
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

ENGINES = ("chromium", "firefox", "webkit")


@dataclass(frozen=True)
class InstalledBrowser:
    id: str
    name: str
    engine: str
    executable: Path
    supported: bool = True


def discover_installed() -> list[InstalledBrowser]:
    found: list[InstalledBrowser] = []
    specs = (
        ("chrome", "Google Chrome", "chromium", "Google Chrome.app/Contents/MacOS/Google Chrome", "Google/Chrome/Application/chrome.exe", ("google-chrome", "google-chrome-stable")),
        ("edge", "Microsoft Edge", "chromium", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Microsoft/Edge/Application/msedge.exe", ("microsoft-edge",)),
        ("brave", "Brave", "chromium", "Brave Browser.app/Contents/MacOS/Brave Browser", "BraveSoftware/Brave-Browser/Application/brave.exe", ("brave-browser",)),
        ("chromium", "Chromium", "chromium", "Chromium.app/Contents/MacOS/Chromium", "Chromium/Application/chrome.exe", ("chromium", "chromium-browser")),
        ("firefox", "Firefox", "firefox", "Firefox.app/Contents/MacOS/firefox", "Mozilla Firefox/firefox.exe", ("firefox",)),
    )
    for browser_id, name, engine, mac, windows, linux in specs:
        candidates: list[Path] = []
        if sys.platform == "darwin":
            candidates = [Path("/Applications") / mac, Path.home() / "Applications" / mac]
        elif sys.platform == "win32":
            candidates = [Path(os.environ[root]) / windows for root in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA") if os.environ.get(root)]
        else:
            candidates = [Path(path) for command in linux if (path := shutil.which(command))]
        executable = next((path for path in candidates if path.is_file()), None)
        if executable is not None:
            found.append(InstalledBrowser(browser_id, name, engine, executable))
    safari = Path("/Applications/Safari.app/Contents/MacOS/Safari")
    if sys.platform == "darwin" and safari.is_file():
        found.append(InstalledBrowser("safari", "Safari", "webkit", safari, supported=False))
    return found


def firefox_preferences(port: int) -> dict[str, object]:
    return {
        "network.proxy.type": 1, "network.proxy.http": "127.0.0.1",
        "network.proxy.http_port": port, "network.proxy.ssl": "127.0.0.1",
        "network.proxy.ssl_port": port, "network.proxy.no_proxies_on": "",
        "network.proxy.allow_hijacking_localhost": True,
        "network.proxy.failover_direct": False, "network.proxy.socks_remote_dns": True,
        "network.trr.mode": 5, "network.dns.disablePrefetch": True,
        "network.prefetch-next": False, "media.peerconnection.enabled": False,
        "browser.shell.checkDefaultBrowser": False, "browser.startup.page": 0,
        "browser.startup.homepage_override.mstone": "ignore",
        "datareporting.healthreport.uploadEnabled": False,
        "datareporting.policy.dataSubmissionEnabled": False,
        "toolkit.telemetry.enabled": False,
    }


def native_arguments(browser: InstalledBrowser, profile: Path, port: int, url: str) -> list[str]:
    if browser.engine == "firefox":
        preferences = firefox_preferences(port)
        (profile / "user.js").write_text(
            "\n".join(f"user_pref({json.dumps(key)}, {json.dumps(value)});" for key, value in preferences.items()),
            encoding="utf-8",
        )
        return [str(browser.executable), "-no-remote", "-new-instance", "-profile", str(profile), url]
    return [
        str(browser.executable), f"--user-data-dir={profile}",
        f"--proxy-server=http://127.0.0.1:{port}", "--proxy-bypass-list=<-loopback>",
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic",
        "--disable-background-networking", "--disable-sync", "--disable-extensions",
        "--no-first-run", "--no-default-browser-check", "--new-window", url,
    ]
