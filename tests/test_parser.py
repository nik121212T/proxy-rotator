"""Single-proxy parsing contracts from SPEC.md section 1."""

import logging
import string
import traceback

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from app.parser import ProxyParseError, parse_line, serialize


def test_parse_line_defaults_to_http() -> None:
    proxy = parse_line("USER01xy:pw1AbC@203.0.113.10:41979")

    assert proxy.scheme == "http"
    assert proxy.username == "USER01xy"
    assert proxy.password.get_secret_value() == "pw1AbC"
    assert proxy.host == "203.0.113.10"
    assert proxy.port == 41979


@pytest.mark.parametrize(
    ("line", "scheme", "host", "port"),
    [
        ("http://user:pw@PROXY.Example:80", "http", "proxy.example", 80),
        ("https://user:pw@proxy.example:443", "https", "proxy.example", 443),
        ("socks5://user:pw@203.0.113.11:1080", "socks5", "203.0.113.11", 1080),
        ("user:pw@[2001:DB8::1]:1080", "http", "2001:db8::1", 1080),
        ("  user:pw@proxy.example:00080\r\n", "http", "proxy.example", 80),
    ],
)
def test_parse_line_accepts_supported_addresses(
    line: str, scheme: str, host: str, port: int
) -> None:
    proxy = parse_line(line)

    assert (proxy.scheme, proxy.host, proxy.port) == (scheme, host, port)


@pytest.mark.parametrize(
    "line",
    [
        "ftp://user:pw@proxy.example:80",
        "us:pw@proxy.example:80",
        f"{'u' * 65}:pw@proxy.example:80",
        "us er:pw@proxy.example:80",
        "üsér:pw@proxy.example:80",
        "user:@proxy.example:80",
        f"user:{'p' * 129}@proxy.example:80",
        "user:p\tw@proxy.example:80",
        "user:p\x7fw@proxy.example:80",
        "user:péw@proxy.example:80",
        "user:pw@proxy.example:0",
        "user:pw@proxy.example:65536",
        "user:pw@proxy.example:+80",
        "user:pw@proxy.example:000080",
        "user:pw@proxy.example:８０",
        "user:pw@256.0.0.1:80",
        "user:pw@127.1:80",
        "user:pw@2001:db8::1:80",
        "user:pw@[invalid]:80",
        "user:pw@[203.0.113.10]:80",
        "user:pw@[fe80::1%en0]:80",
        "user:pw@-proxy.example:80",
        "user:pw@proxy-.example:80",
        "user:pw@proxy..example:80",
        "user:pw@proxy_example:80",
        f"user:pw@{'a' * 64}.example:80",
        f"user:pw@{'.'.join(['a' * 63] * 4)}:80",
        "user:pw@proxy.example/path:80",
    ],
)
def test_parse_line_rejects_invalid_grammar(line: str) -> None:
    with pytest.raises(ProxyParseError):
        parse_line(line)


def test_parsed_proxy_masks_credentials_in_logs(caplog: pytest.LogCaptureFixture) -> None:
    password = "synthetic-sensitive-value"
    proxy = parse_line(f"user:{password}@proxy.example:80")

    with caplog.at_level(logging.INFO):
        logging.getLogger(__name__).info("proxy=%s detail=%r", proxy, proxy)

    assert password not in caplog.text
    assert "user:***@proxy.example:80" in caplog.text
    assert password not in proxy.model_dump_json()


@pytest.mark.parametrize(
    "template",
    [
        "user:{password}@proxy.example:not-a-port",
        "user:{password}@proxy.example:70000",
        "user:{password}@[not-an-ip]:80",
        "user:{password}:extra@proxy.example:80",
        "user:{password}@extra@proxy.example:80",
        "user:{password}@bad-host!:80",
    ],
)
def test_parse_errors_do_not_expose_credentials(template: str) -> None:
    password = "synthetic-sensitive-value"
    line = template.format(password=password)

    with pytest.raises(ProxyParseError) as error:
        parse_line(line)

    rendered = "".join(traceback.format_exception(error.value))
    assert password not in rendered
    assert password not in repr(error.value)


@pytest.mark.parametrize(
    "line",
    [
        "http://user:p# /%?[]\\w@proxy.example:80",
        "https://user: password @proxy.example:65535",
        "socks5://user:pw@[2001:db8::1]:1",
        f"http://{'u' * 64}:{'p' * 128}@proxy.example:8080",
    ],
)
def test_serialize_preserves_literal_password_and_address(line: str) -> None:
    proxy = parse_line(line)

    assert serialize(proxy) == line
    assert parse_line(serialize(proxy)) == proxy


_HOSTS = st.one_of(
    st.ip_addresses(v=4).map(lambda address: (str(address), str(address))),
    st.ip_addresses(v=6).map(lambda address: (f"[{address.exploded}]", str(address))),
    st.lists(
        st.text(string.ascii_letters + string.digits, min_size=1, max_size=20),
        min_size=1,
        max_size=4,
    ).map(lambda labels: ("p" + ".".join(labels), ("p" + ".".join(labels)).lower())),
)


@settings(max_examples=300, derandomize=True, database=None)
@given(
    scheme=st.sampled_from(["", "http://", "https://", "socks5://"]),
    username=st.text(string.ascii_letters + string.digits + "_.-", min_size=3, max_size=64),
    password=st.text(
        "".join(chr(code) for code in range(32, 127) if chr(code) not in ":@"),
        min_size=1,
        max_size=128,
    ),
    host=_HOSTS,
    port=st.integers(min_value=1, max_value=65535),
)
def test_parse_serialize_round_trip(
    scheme: str, username: str, password: str, host: tuple[str, str], port: int
) -> None:
    proxy = parse_line(f"{scheme}{username}:{password}@{host[0]}:{port}")

    assert proxy.username == username
    assert proxy.password.get_secret_value() == password
    assert proxy.scheme == (scheme.removesuffix("://") or "http")
    assert proxy.host == host[1]
    assert proxy.port == port
    assert parse_line(serialize(proxy)) == proxy
