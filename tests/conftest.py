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


@pytest_asyncio.fixture(autouse=True)
async def no_leaked_tasks() -> AsyncIterator[None]:
    import asyncio

    baseline = asyncio.all_tasks()
    yield
    await asyncio.sleep(0)
    leaked = asyncio.all_tasks() - baseline - {asyncio.current_task()}
    descriptions = [repr(task.get_coro()) for task in leaked]
    for task in leaked:
        task.cancel()
    await asyncio.gather(*leaked, return_exceptions=True)
    assert not leaked, f'Background tasks survived teardown: {descriptions}'
