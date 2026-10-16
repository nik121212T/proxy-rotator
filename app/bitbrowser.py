"""Local BitBrowser API, encrypted connection settings and owned persistent profiles."""

import asyncio
import json
import uuid
from typing import Any
from urllib.parse import quote, urlsplit

import aiohttp
import aiosqlite
from pydantic import SecretStr

from app import provider_store


class BitBrowserError(ValueError):
    """Only static user-facing messages; never reflect API payloads or keys."""


class BitBrowserClient:
    def __init__(self, conn: aiosqlite.Connection, key: bytes) -> None:
        self.conn, self.key = conn, key
        self.port = 54345
        self.token = SecretStr("")
        self.owner = uuid.uuid4().hex
        self.configured = False

    @classmethod
    async def load(cls, conn: aiosqlite.Connection, key: bytes) -> "BitBrowserClient":
        await provider_store.ensure_schema(conn)
        await conn.execute(
            "CREATE TABLE IF NOT EXISTS bitbrowser_profiles (slot TEXT PRIMARY KEY, profile_id TEXT NOT NULL UNIQUE)"
        )
        await conn.commit()
        client = cls(conn, key)
        saved = await provider_store.get_token(conn, "bitbrowser-config", key)
        if saved:
            try:
                data = json.loads(saved.get_secret_value())
                port = int(data["port"])
                if not 1 <= port <= 65535 or not isinstance(data["owner"], str):
                    raise ValueError
                client.port, client.token, client.owner = (
                    port,
                    SecretStr(data["token"]),
                    data["owner"],
                )
                client.configured = True
            except (ValueError, KeyError, TypeError):
                raise BitBrowserError(
                    "Configuration BitBrowser illisible. Reconnectez BitBrowser."
                ) from None
        return client

    async def request(
        self,
        path: str,
        body: dict[str, Any],
        *,
        port: int | None = None,
        token: SecretStr | None = None,
    ) -> dict[str, Any]:
        headers = {"x-api-key": (token if token is not None else self.token).get_secret_value()}
        try:
            async with aiohttp.ClientSession(  # noqa: SIM117
                timeout=aiohttp.ClientTimeout(total=35), trust_env=False
            ) as session:
                async with session.post(
                    f"http://127.0.0.1:{port or self.port}{path}",
                    json=body,
                    headers=headers,
                    allow_redirects=False,
                ) as response:
                    if response.status in {401, 403}:
                        raise BitBrowserError(
                            "Clé API BitBrowser refusée. Vérifiez la clé et les autorisations du compte."
                        )
                    if response.status != 200:
                        raise BitBrowserError(
                            "BitBrowser a refusé l’opération. Vérifiez son compte et son API locale."
                        )
                    try:
                        raw = await response.content.readexactly(2 * 1024 * 1024 + 1)
                    except asyncio.IncompleteReadError as exc:
                        raw = exc.partial
                    if len(raw) > 2 * 1024 * 1024:
                        raise BitBrowserError("Réponse BitBrowser trop volumineuse.")
                    payload = json.loads(raw)
        except (aiohttp.ClientError, TimeoutError, OSError):
            raise BitBrowserError(
                "BitBrowser inaccessible. Ouvrez-le, connectez votre compte et activez son API locale."
            ) from None
        except (ValueError, UnicodeError) as exc:
            if isinstance(exc, BitBrowserError):
                raise
            raise BitBrowserError("Réponse BitBrowser invalide.") from None
        if not isinstance(payload, dict) or payload.get("success") is not True:
            raise BitBrowserError(
                "BitBrowser a refusé l’opération. Vérifiez la clé API, les droits du compte et le nombre de profils disponibles."
            )
        data = payload.get("data", {})
        if not isinstance(data, dict):
            raise BitBrowserError("Réponse BitBrowser invalide.")
        return data

    async def configure(self, port: int, token: SecretStr | None) -> None:
        if not 1 <= port <= 65535:
            raise BitBrowserError("Port API invalide.")
        selected = token if token is not None else self.token
        await self.request("/browser/list", {"page": 0, "pageSize": 1}, port=port, token=selected)
        saved = SecretStr(
            json.dumps({"port": port, "token": selected.get_secret_value(), "owner": self.owner})
        )
        await provider_store.set_token(self.conn, "bitbrowser-config", saved, self.key)
        self.port, self.token, self.configured = port, selected, True

    async def check_owned(self, identifier: str) -> None:
        async with self.conn.execute(
            "SELECT 1 FROM bitbrowser_profiles WHERE profile_id = ?", (identifier,)
        ) as cursor:
            owned = await cursor.fetchone()
        if owned is None:
            raise BitBrowserError("Ce profil n’appartient pas au gestionnaire.")
        detail = await self.request("/browser/detail", {"id": identifier})
        if detail.get("remark") != "proxy-rotator:" + self.owner:
            raise BitBrowserError(
                "Ce profil n’appartient plus au gestionnaire. Vérifiez le compte BitBrowser connecté."
            )

    async def running(self, identifier: str) -> bool:
        data = await self.request("/browser/pids", {"ids": [identifier]})
        return bool(data.get(identifier))

    async def prepare(self, proxy_id: int | None, port: int, url: str) -> str:
        if not self.configured:
            raise BitBrowserError(
                "Connectez BitBrowser dans le panneau ci-dessous avant de l’utiliser."
            )
        slot = "rotation" if proxy_id is None else f"proxy:{proxy_id}"
        async with self.conn.execute(
            "SELECT profile_id FROM bitbrowser_profiles WHERE slot = ?", (slot,)
        ) as cursor:
            row = await cursor.fetchone()
        proxy = {
            "proxyMethod": 2,
            "proxyType": "http",
            "host": "127.0.0.1",
            "port": port,
            "proxyUserName": "",
            "proxyPassword": "",
            "isGlobalProxyInfo": False,
        }
        if row:
            identifier = str(row[0])
            await self.check_owned(identifier)
            if await self.running(identifier):
                raise BitBrowserError(
                    "Ce profil BitBrowser est déjà ouvert. Fermez sa fenêtre avant de le relancer ici."
                )
            await self.request(
                "/browser/proxy/update", {"ids": [identifier], "ipCheckService": "ip-api", **proxy}
            )
            await self.request(
                "/browser/update/partial",
                {"ids": [identifier], "url": quote(url, safe=""), "browserFingerPrint": {}},
            )
            return identifier
        data = await self.request(
            "/browser/update",
            {
                "name": "Proxy Rotator · "
                + ("Rotation" if proxy_id is None else f"Proxy #{proxy_id}"),
                "remark": "proxy-rotator:" + self.owner,
                "platform": url,
                "platformIcon": urlsplit(url).hostname or "",
                "url": quote(url, safe=""),
                "userName": "",
                "password": "",
                "browserFingerPrint": {},
                "stopWhileNetError": True,
                "syncTabs": False,
                "syncCookies": False,
                **proxy,
            },
        )
        created_id = data.get("id")
        if not isinstance(created_id, str) or not created_id or len(created_id) > 128:
            raise BitBrowserError(
                "Profil créé sans identifiant exploitable. Vérifiez BitBrowser avant de réessayer."
            )
        identifier = created_id
        await self.conn.execute(
            "INSERT INTO bitbrowser_profiles(slot, profile_id) VALUES (?, ?)", (slot, identifier)
        )
        await self.conn.commit()
        return identifier

    async def open(self, identifier: str) -> None:
        await self.check_owned(identifier)
        await self.request(
            "/browser/open",
            {
                "id": identifier,
                "args": [
                    "--proxy-bypass-list=<-loopback>",
                    "--disable-quic",
                    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
                ],
            },
        )

    async def close(self, identifier: str) -> None:
        await self.check_owned(identifier)
        if not await self.running(identifier):
            return
        await self.request("/browser/close", {"id": identifier})
        for _ in range(5):
            if not await self.running(identifier):
                return
            await asyncio.sleep(1)
        raise BitBrowserError(
            "BitBrowser n’a pas confirmé la fermeture. Fermez sa fenêtre et réessayez."
        )
