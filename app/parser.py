"""Pure parsing of individual proxy records."""

import ipaddress
import re
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, SecretStr

Scheme = Literal["http", "https", "socks5"]
_LINE = re.compile(
    r"(?:(?P<scheme>http|https|socks5)://)?"
    r"(?P<username>[A-Za-z0-9_.-]{3,64}):"
    r"(?P<password>[\x20-\x7e]{1,128})@"
    r"(?P<host>\[[^\]\s]+\]|[^:\s]+):(?P<port>[0-9]{1,5})"
)
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class ProxyParseError(ValueError):
    """Invalid proxy input, with a credential-free error message."""


def _normalize_host(host: str) -> str:
    host = host.lower()
    if host.startswith("["):
        address = host[1:-1]
        if "%" in address:
            raise ValueError("Scoped IPv6 is not supported")
        return str(ipaddress.IPv6Address(address))
    if "." in host and all(character in "0123456789." for character in host):
        return str(ipaddress.IPv4Address(host))
    name = host.removesuffix(".")
    if len(name) > 253 or not all(_HOST_LABEL.fullmatch(label) for label in name.split(".")):
        raise ValueError("Invalid hostname")
    return host


class ParsedProxy(BaseModel):
    """In-memory credentials; never persist without encryption."""

    model_config = ConfigDict(frozen=True, hide_input_in_errors=True)

    scheme: Scheme
    username: str
    password: SecretStr
    host: str
    port: int

    def __str__(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"{self.scheme}://{self.username}:***@{host}:{self.port}"

    def __repr__(self) -> str:
        return f"ParsedProxy({str(self)!r})"


def parse_line(line: str) -> ParsedProxy:
    """Parse a record; empty/comment lines belong to the bulk importer.

    Password characters are printable ASCII, excluding ':' and '@'. They
    are literal, not URL-decoded. Errors never contain the supplied record.
    """
    match = _LINE.fullmatch(line.strip())
    if match is None:
        raise ProxyParseError("Invalid proxy format")
    password = match["password"]
    if ":" in password or "@" in password:
        raise ProxyParseError("Invalid proxy credentials")
    port = int(match["port"])
    if not 1 <= port <= 65535:
        raise ProxyParseError("Invalid proxy port")
    try:
        host = _normalize_host(match["host"])
    except ValueError:
        raise ProxyParseError("Invalid proxy host") from None
    return ParsedProxy(
        scheme=cast(Scheme, match["scheme"] or "http"),
        username=match["username"],
        password=SecretStr(password),
        host=host,
        port=port,
    )


def serialize(proxy: ParsedProxy) -> str:
    """Return the round-trippable record, including its raw password.

    This explicit credential boundary is for parsing/transport only. Never
    log or persist its result; use str(proxy) for a masked representation.
    """
    host = f"[{proxy.host}]" if ":" in proxy.host else proxy.host
    return (
        f"{proxy.scheme}://{proxy.username}:{proxy.password.get_secret_value()}"
        f"@{host}:{proxy.port}"
    )
