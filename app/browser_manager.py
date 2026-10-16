"""Application-owned browser sessions, private gateways and on-demand engines."""

import asyncio
import contextlib
import logging
import os
import shutil
import signal
import stat
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from playwright.async_api import BrowserContext, Error, Playwright, async_playwright

from app.bitbrowser import BitBrowserClient, BitBrowserError
from app.browser_catalog import ENGINES, discover_installed, firefox_preferences, native_arguments
from app.browser_storage import delete_engine, engine_folders, installed_size
from app.burn_detector import BurnDetector
from app.config import Settings
from app.gateway_http import HttpGateway
from app.models import Proxy
from app.pool import Pool
from app.rotator import ActivePool, Rotator

logger = logging.getLogger(__name__)


class BrowserError(ValueError):
    """Safe user-facing errors only, never upstream credentials or driver output."""


class FixedRotator(Rotator):
    def __init__(self, proxy_id: int) -> None:
        super().__init__("round_robin")
        self.proxy_id = proxy_id

    def pick(self, pool: ActivePool, *, client_ip: str = "", target_domain: str = "") -> Proxy | None:
        return next((p for p in pool.active() if p.id == self.proxy_id), None)


@dataclass
class BrowserSession:
    id: str
    name: str
    proxy_id: int | None
    profile: Path
    gateway: HttpGateway
    process: asyncio.subprocess.Process | None = None
    context: BrowserContext | None = None
    watcher: asyncio.Task[None] | None = None
    close_error: str | None = None
    engine: str | None = None
    bitbrowser_id: str | None = None


def remove_profile(profile: Path) -> None:
    """Remove only an owned temporary profile, including Windows read-only files."""
    def writable_retry(function, path, exc_info):  # type: ignore[no-untyped-def]
        if not isinstance(exc_info[1], PermissionError):
            raise exc_info[1]
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        function(path)

    try:
        shutil.rmtree(profile, onerror=writable_retry)
    except FileNotFoundError:
        if profile.exists():
            raise
    if profile.exists():
        raise OSError("Profile remains")


async def terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill", "/PID", str(process.pid), "/T", "/F",  # noqa: S603,S607 - owned PID only
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(killer.wait(), 10)
        if killer.returncode and process.returncode is None:
            raise BrowserError("Fermeture du navigateur refusée par Windows. Réessayez après avoir fermé sa fenêtre.")
    else:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            if sys.platform == "win32":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


class BrowserManager:
    def __init__(self, pool: Pool, rotator: Rotator, key: bytes, root: Path, *, headless: bool = False, settings: Settings | None = None, bitbrowser: BitBrowserClient | None = None) -> None:
        self.pool, self.rotator, self.key = pool, rotator, key
        self.root = root
        self.settings = settings
        self.bitbrowser = bitbrowser
        self.headless = headless
        self.sessions: dict[str, BrowserSession] = {}
        self.installs: dict[str, str] = {}
        self.install_errors: dict[str, str] = {}
        self._install_tasks: dict[str, asyncio.Task[None]] = {}
        self._driver: Playwright | None = None
        self._lock = asyncio.Lock()
        self._driver_lock = asyncio.Lock()
        self._closing = False
        self._removing: set[str] = set()
        self._sizes: dict[str, tuple[float, int | None]] = {}

    async def _playwright(self) -> Playwright:
        async with self._driver_lock:
            if self._driver is None:
                self._driver = await async_playwright().start()
            return self._driver

    async def catalog(self) -> dict[str, object]:
        installed = await asyncio.to_thread(discover_installed)
        driver = await self._playwright()
        dedicated = []
        for engine in ENGINES:
            executable = Path(getattr(driver, engine).executable_path)
            ready = executable.is_file()
            cached = self._sizes.get(engine)
            if cached is None or time.monotonic() - cached[0] > 30:
                try:
                    size = await asyncio.to_thread(installed_size, engine_folders(engine, executable))
                except (ValueError, OSError):
                    size = None
                self._sizes[engine] = (time.monotonic(), size)
            else:
                size = cached[1]
            in_use = any(s.engine == engine for s in self.sessions.values())
            dedicated.append({"id": engine, "name": {"chromium": "Chromium", "firefox": "Firefox", "webkit": "WebKit"}[engine], "ready": ready, "status": self.installs.get(engine, "ready" if ready else "missing"), "error": self.install_errors.get(engine), "size_bytes": size,
                              "removing": engine in self._removing,
                              "can_remove": (ready or bool(size)) and not in_use and engine not in self._install_tasks and engine not in self._removing,
                              "in_use": in_use})
        return {
            "installed": [{"id": b.id, "name": b.name, "engine": b.engine, "supported": b.supported} for b in installed],
            "dedicated": dedicated,
            "bitbrowser": {"configured": bool(self.bitbrowser and self.bitbrowser.configured),
                           "port": self.bitbrowser.port if self.bitbrowser else 54345},
        }

    def session_views(self) -> list[dict[str, object]]:
        exits = self.pool.exit_observations()
        return [{
            "id": s.id, "name": s.name, "proxy_id": s.proxy_id,
            "close_error": s.close_error,
            "mode": "fixed" if s.proxy_id is not None else "rotation",
            "exit_ip": str(exits[s.proxy_id].ip) if s.proxy_id in exits else None,
            "available": any(p.id == s.proxy_id for p in self.pool.active()) if s.proxy_id is not None else bool(self.rotator.eligible(self.pool)),
        } for s in self.sessions.values()]

    def install(self, engine: str) -> None:
        if engine not in ENGINES:
            raise BrowserError("Moteur inconnu.")
        if self._closing:
            raise BrowserError("Application en cours d’arrêt.")
        if engine in self._removing:
            raise BrowserError("Suppression en cours. Patientez avant de télécharger ce moteur.")
        if engine in self._install_tasks:
            return
        self.install_errors.pop(engine, None)
        self.installs[engine] = "installing"
        self._install_tasks[engine] = asyncio.create_task(self._install(engine))

    async def _install(self, engine: str) -> None:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "playwright", "install", engine,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                start_new_session=sys.platform != "win32",
            )
            code = await asyncio.wait_for(process.wait(), 900)
            self.installs[engine] = "ready" if code == 0 else "error"
            if code:
                self.install_errors[engine] = (
                    "Téléchargement échoué. Vérifiez Internet, l’espace disque et les droits du dossier, puis réessayez."
                )
        except TimeoutError:
            self.installs[engine] = "error"
            self.install_errors[engine] = "Téléchargement interrompu après 15 minutes. Vérifiez Internet puis réessayez."
        except OSError:
            self.installs[engine] = "error"
            self.install_errors[engine] = "Installation impossible à démarrer. Vérifiez Python et Playwright."
        finally:
            try:
                if process is not None and process.returncode is None:
                    await terminate_process(process)
            except (BrowserError, OSError, TimeoutError):
                self.installs[engine] = "error"
                self.install_errors[engine] = "Arrêt du téléchargement incomplet. Fermez l’application avant de réessayer."
                logger.warning("browser_install_cleanup_pending engine=%s", engine)
            finally:
                if self.installs.get(engine) == "installing":
                    self.installs[engine] = "error"
                    self.install_errors[engine] = "Téléchargement interrompu. Réessayez à la prochaine ouverture."
                self._install_tasks.pop(engine, None)
                self._sizes.pop(engine, None)

    async def uninstall(self, engine: str) -> None:
        if engine not in ENGINES:
            raise BrowserError("Moteur inconnu.")
        async with self._lock:
            if self._closing or engine in self._install_tasks or engine in self._removing:
                raise BrowserError("Une opération est en cours sur ce moteur. Réessayez ensuite.")
            if any(s.engine == engine for s in self.sessions.values()):
                raise BrowserError("Fermez les sessions de ce moteur avant de le supprimer.")
            self._removing.add(engine)
            try:
                driver = await self._playwright()
                folders = engine_folders(engine, Path(getattr(driver, engine).executable_path))
                await asyncio.to_thread(delete_engine, folders)
                self.installs.pop(engine, None)
                self.install_errors.pop(engine, None)
            except (ValueError, OSError):
                raise BrowserError("Suppression impossible. Fermez les fenêtres de ce moteur et réessayez.") from None
            finally:
                self._removing.discard(engine)
                self._sizes.pop(engine, None)

    async def launch(self, source: Literal["installed", "dedicated", "bitbrowser"], browser_id: str, proxy_id: int | None, url: str) -> str:
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise BrowserError("URL invalide.") from None
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or len(url) > 2048:
            raise BrowserError("Saisissez une URL http:// ou https:// sans identifiants.")
        async with self._lock:
            if self._closing or len(self.sessions) >= 8:
                raise BrowserError("Huit sessions maximum ; fermez une session avant de continuer.")
            if proxy_id is not None and not any(p.id == proxy_id for p in self.pool.active()):
                raise BrowserError("Ce proxy n’est plus disponible.")
            if not self.pool.active():
                raise BrowserError("Aucun proxy disponible. Importez et testez vos proxies.")
            if proxy_id is None and not self.rotator.eligible(self.pool):
                raise BrowserError("Aucun proxy disponible pour la sélection de rotation actuelle.")
            if source == "bitbrowser":
                if browser_id != "bitbrowser" or not self.bitbrowser or not self.bitbrowser.configured:
                    raise BrowserError("Connectez BitBrowser dans le panneau ci-dessous.")
                if any(s.bitbrowser_id and s.proxy_id == proxy_id for s in self.sessions.values()):
                    raise BrowserError("Ce profil BitBrowser possède déjà une session ouverte.")
            installed = next((b for b in await asyncio.to_thread(discover_installed) if b.id == browser_id), None) if source == "installed" else None
            if source == "installed" and (installed is None or not installed.supported):
                raise BrowserError("Navigateur absent ou incompatible avec un proxy isolé. Utilisez un moteur dédié.")
            if source == "dedicated" and browser_id not in ENGINES:
                raise BrowserError("Moteur inconnu.")
            identifier = uuid.uuid4().hex
            profile = self.root / identifier
            await asyncio.to_thread(profile.mkdir, parents=True, mode=0o700)
            config = self.settings
            timeout = config.gateway_timeout_s if config else 15.0
            gateway = HttpGateway(
                self.pool, FixedRotator(proxy_id) if proxy_id is not None else self.rotator, self.key,
                port=0, timeout_s=timeout,
                max_retry_per_request=config.max_retry_per_request if config else 2,
                max_connections=config.gateway_max_connections if config else 256,
                burn_detector=BurnDetector(timeout_s=timeout, inspect_body=config.burn_inspect_body if config else True),
            )
            session = BrowserSession(identifier, installed.name if installed else browser_id.capitalize(), proxy_id, profile, gateway, engine=browser_id if source == "dedicated" else None)
            try:
                await gateway.start()
                bound = gateway.status("").bound
                assert bound is not None
                port = int(bound.rsplit(":", 1)[1])
                if source == "bitbrowser":
                    assert self.bitbrowser is not None
                    session.name = "BitBrowser · " + ("Rotation" if proxy_id is None else f"Proxy #{proxy_id}")
                    session.bitbrowser_id = await self.bitbrowser.prepare(proxy_id, port, url)
                    await self.bitbrowser.open(session.bitbrowser_id)
                elif installed:
                    args = await asyncio.to_thread(native_arguments, installed, profile, port, url)
                    session.process = await asyncio.create_subprocess_exec(
                        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
                        start_new_session=sys.platform != "win32",
                    )
                    await asyncio.sleep(0.4)
                    if session.process.returncode is not None:
                        raise BrowserError("Le navigateur s’est fermé au lancement. Essayez le moteur dédié.")
                else:
                    driver = await self._playwright()
                    browser_type = getattr(driver, browser_id)
                    if not Path(browser_type.executable_path).is_file():
                        raise BrowserError("Téléchargez ce moteur avant de l’ouvrir.")
                    kwargs: dict[str, object] = {}
                    if browser_id == "chromium":
                        kwargs["args"] = ["--proxy-bypass-list=<-loopback>", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", "--disable-quic"]
                    if browser_id == "firefox":
                        kwargs["firefox_user_prefs"] = firefox_preferences(port)
                    session.context = await browser_type.launch_persistent_context(
                        str(profile), headless=self.headless, proxy={"server": f"http://127.0.0.1:{port}"},
                        timeout=30000, **kwargs,
                    )
                    assert session.context is not None
                    page = session.context.pages[0] if session.context.pages else await session.context.new_page()
                    # A proxy may fail after opening; keep the window and its error page.
                    with contextlib.suppress(Error):
                        await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                self.sessions[identifier] = session
                session.watcher = asyncio.create_task(self._watch(session))
                return identifier
            except BaseException as exc:
                try:
                    await self._dispose(session)
                except BrowserError:
                    session.close_error = "Nettoyage incomplet après un échec de lancement. Réessayez la fermeture."
                    self.sessions[identifier] = session
                    raise
                if isinstance(exc, BitBrowserError):
                    raise BrowserError(str(exc)) from None
                if isinstance(exc, Error | OSError):
                    raise BrowserError("Ouverture impossible. Vérifiez l’installation du navigateur et l’accès au bureau. Sous Windows, vérifiez aussi les composants Visual C++ et Media Foundation (voir le guide Windows).") from None
                raise

    async def _watch(self, session: BrowserSession) -> None:
        try:
            if session.bitbrowser_id:
                assert self.bitbrowser is not None
                while await self.bitbrowser.running(session.bitbrowser_id):
                    await asyncio.sleep(3)
            elif session.process:
                await session.process.wait()
            elif session.context:
                while session.context.browser and session.context.browser.is_connected():
                    await asyncio.sleep(1)
            await self.close_session(session.id)
        except asyncio.CancelledError:
            pass
        except BitBrowserError:
            await session.gateway.stop()
            session.close_error = "Suivi BitBrowser interrompu. Connexion proxy arrêtée ; réessayez la fermeture."
        except BrowserError:
            logger.warning("browser_cleanup_pending session=%s", session.id)

    async def _dispose(self, session: BrowserSession) -> None:
        try:
            if session.bitbrowser_id:
                assert self.bitbrowser is not None
                await self.bitbrowser.close(session.bitbrowser_id)
            if session.context:
                await session.context.close()
            if session.process:
                await terminate_process(session.process)
        except BitBrowserError as exc:
            raise BrowserError(str(exc)) from None
        except (Error, OSError, TimeoutError) as exc:
            raise BrowserError("Fermeture du navigateur incomplète. Réessayez après avoir fermé sa fenêtre.") from exc
        finally:
            # Stop forwarding even if browser termination failed.
            await session.gateway.stop()
        for attempt in range(3):
            try:
                await asyncio.to_thread(remove_profile, session.profile)
                return
            except OSError:
                if attempt == 2:
                    raise BrowserError("Le profil temporaire n’a pas pu être supprimé. Fermez sa fenêtre puis réessayez.") from None
                await asyncio.sleep(0.15)

    async def close_session(self, identifier: str) -> None:
        async with self._lock:
            session = self.sessions.get(identifier)
            if session is None:
                return
            if session.watcher and session.watcher is not asyncio.current_task():
                session.watcher.cancel()
                await asyncio.gather(session.watcher, return_exceptions=True)
            try:
                await self._dispose(session)
            except BrowserError as exc:
                session.close_error = str(exc)
                raise
            self.sessions.pop(identifier, None)

    async def stop(self) -> None:
        self._closing = True
        tasks = list(self._install_tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            identifiers = list(self.sessions)
        for identifier in identifiers:
            try:
                await self.close_session(identifier)
            except BrowserError:
                logger.warning("browser_cleanup_pending session=%s", identifier)
        if self._driver:
            await self._driver.stop()
            self._driver = None
