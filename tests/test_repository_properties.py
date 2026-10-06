"""Generated insert/list round trips, with a fresh database for every example."""

import string

import pytest
from cryptography.fernet import Fernet
from hypothesis import given, settings
from hypothesis import strategies as st

from app.crypto import decrypt_password
from app.db import get_db
from app.parser import parse_line
from app.repository import insert_proxy, list_proxies


@settings(max_examples=100, deadline=None, derandomize=True, database=None)
@given(
    scheme=st.sampled_from(["http", "https", "socks5"]),
    username=st.text(string.ascii_letters + string.digits + "_.-", min_size=3, max_size=64),
    password=st.text(
        "".join(chr(code) for code in range(32, 127) if chr(code) not in ":@"),
        min_size=1,
        max_size=128,
    ),
    host=st.one_of(
        st.ip_addresses(v=4).map(str),
        st.ip_addresses(v=6).map(lambda address: f"[{address}]"),
        st.sampled_from(["PROXY.example", "proxy-1.example.", "localhost"]),
    ),
    port=st.integers(1, 65535),
    label=st.one_of(st.none(), st.text(max_size=50)),
)
async def test_insert_list_round_trip(
    tmp_path_factory: pytest.TempPathFactory,
    scheme: str,
    username: str,
    password: str,
    host: str,
    port: int,
    label: str | None,
) -> None:
    # Session-scoped factory avoids reusing a function-scoped DB between Hypothesis examples.
    path = tmp_path_factory.mktemp("repository-example") / "proxies.db"
    key = Fernet.generate_key()
    parsed = parse_line(f"{scheme}://{username}:{password}@{host}:{port}")

    async with get_db(path) as conn:
        inserted = await insert_proxy(conn, parsed, key, label=label)
        assert await list_proxies(conn) == [inserted]
    async with get_db(path) as reopened:
        [loaded] = await list_proxies(reopened)
    assert loaded == inserted
    assert (loaded.scheme, loaded.host, loaded.port, loaded.username) == (
        parsed.scheme,
        parsed.host,
        parsed.port,
        parsed.username,
    )
    assert loaded.label == label
    assert decrypt_password(loaded.password_enc.get_secret_value(), key) == parsed.password
