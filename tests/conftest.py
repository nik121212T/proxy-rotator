"""Real, per-test databases; no SQLite mocks."""

from collections.abc import AsyncIterator
from pathlib import Path

import aiosqlite
import pytest_asyncio

from app.db import get_db


@pytest_asyncio.fixture
async def db(tmp_path: Path) -> AsyncIterator[aiosqlite.Connection]:
    async with get_db(tmp_path / "proxies.db") as conn:
        yield conn
