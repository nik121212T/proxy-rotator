"""Round-trip and error handling tests for the encrypted provider token store."""

from __future__ import annotations

import aiosqlite
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.provider_store import ensure_schema, get_token, set_token


async def test_token_round_trip(db: aiosqlite.Connection) -> None:
    await ensure_schema(db)
    key = Fernet.generate_key()
    await set_token(db, "dichvusocks", SecretStr("abc123"), key)
    recovered = await get_token(db, "dichvusocks", key)
    assert recovered is not None
    assert recovered.get_secret_value() == "abc123"


async def test_token_is_stored_encrypted(db: aiosqlite.Connection) -> None:
    """The raw blob never contains the plaintext (encryption at rest)."""
    await ensure_schema(db)
    key = Fernet.generate_key()
    await set_token(db, "dichvusocks", SecretStr("abc123"), key)
    async with db.execute(
        "SELECT token_enc FROM provider_tokens WHERE provider = ?", ("dichvusocks",)
    ) as cursor:
        row = await cursor.fetchone()
    assert row is not None
    assert b"abc123" not in row[0]
    assert row[0].startswith(b"gAAAAA")  # Fernet token prefix.


async def test_setting_blank_or_none_token_deletes_the_row(db: aiosqlite.Connection) -> None:
    await ensure_schema(db)
    key = Fernet.generate_key()
    await set_token(db, "dichvusocks", SecretStr("abc"), key)
    assert await get_token(db, "dichvusocks", key) is not None
    await set_token(db, "dichvusocks", SecretStr("   "), key)
    assert await get_token(db, "dichvusocks", key) is None
    await set_token(db, "dichvusocks", SecretStr("def"), key)
    await set_token(db, "dichvusocks", None, key)
    assert await get_token(db, "dichvusocks", key) is None


async def test_overwrite_updates_in_place(db: aiosqlite.Connection) -> None:
    await ensure_schema(db)
    key = Fernet.generate_key()
    await set_token(db, "dichvusocks", SecretStr("one"), key)
    await set_token(db, "dichvusocks", SecretStr("two"), key)
    recovered = await get_token(db, "dichvusocks", key)
    assert recovered is not None and recovered.get_secret_value() == "two"
    async with db.execute("SELECT COUNT(*) FROM provider_tokens") as cursor:
        row = await cursor.fetchone()
    assert row is not None and row[0] == 1


async def test_wrong_key_returns_none_without_crashing(db: aiosqlite.Connection) -> None:
    await ensure_schema(db)
    key_a = Fernet.generate_key()
    key_b = Fernet.generate_key()
    await set_token(db, "dichvusocks", SecretStr("abc"), key_a)
    assert await get_token(db, "dichvusocks", key_b) is None
