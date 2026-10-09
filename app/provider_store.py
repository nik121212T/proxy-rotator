"""Encrypted key/value store for third-party provider credentials.

Values live in a single SQLite table ``provider_tokens`` and are encrypted
at rest with the application's Fernet key so plaintext never touches disk.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import aiosqlite
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

logger = logging.getLogger(__name__)


async def ensure_schema(conn: aiosqlite.Connection) -> None:
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS provider_tokens (
            provider    TEXT PRIMARY KEY,
            token_enc   BLOB NOT NULL,
            updated_at  TEXT NOT NULL
        )
        """
    )
    await conn.commit()


async def set_token(
    conn: aiosqlite.Connection, provider: str, token: SecretStr | None, key: bytes
) -> None:
    if token is None or not token.get_secret_value().strip():
        await conn.execute("DELETE FROM provider_tokens WHERE provider = ?", (provider,))
        await conn.commit()
        return
    enc = Fernet(key).encrypt(token.get_secret_value().encode("utf-8"))
    now = datetime.now(tz=UTC).isoformat()
    await conn.execute(
        """
        INSERT INTO provider_tokens (provider, token_enc, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(provider) DO UPDATE SET
            token_enc  = excluded.token_enc,
            updated_at = excluded.updated_at
        """,
        (provider, enc, now),
    )
    await conn.commit()


async def get_token(
    conn: aiosqlite.Connection, provider: str, key: bytes
) -> SecretStr | None:
    async with conn.execute(
        "SELECT token_enc FROM provider_tokens WHERE provider = ?", (provider,)
    ) as cursor:
        row = await cursor.fetchone()
    if row is None:
        return None
    try:
        plain = Fernet(key).decrypt(row[0]).decode("utf-8")
    except (InvalidToken, ValueError):
        logger.warning("provider_token_decrypt_failed provider=%s", provider)
        return None
    return SecretStr(plain)
