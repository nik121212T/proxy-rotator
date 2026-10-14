"""Pure parsing of individual proxy records."""

import ipaddress
import re
from io import StringIO
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr

Scheme = Literal["http", "https", "socks5"]
MAX_IMPORT_LINES = 2000
_LINE = re.compile(
    r"(?:(?P<scheme>http|https|socks5)://)?"
    r"(?:(?P<username>[A-Za-z0-9_.-]{3,64}):"
    r"(?P<password>[\x20-\x7e]{1,128})@)?"
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
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.scheme}://{auth}{host}:{self.port}"

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
    password = match["password"] or ""
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
        username=match["username"] or "",
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
    auth = f"{proxy.username}:{proxy.password.get_secret_value()}@" if proxy.username else ""
    return f"{proxy.scheme}://{auth}{host}:{proxy.port}"


class ImportLineError(BaseModel):
    """Identify a rejected record without returning its credentials."""

    line: int
    reason: str


class ImportReport(BaseModel):
    """Public import counts plus accepted records for the storage caller."""

    imported: int = 0
    with_auth: int = Field(default=0, ge=0)
    without_auth: int = Field(default=0, ge=0)
    skipped: int = 0
    errors: list[ImportLineError] = Field(default_factory=list)
    proxies: list[ParsedProxy] = Field(default_factory=list, exclude=True, repr=False)


def parse_bulk(text: str) -> ImportReport:
    """Parse an in-memory list of records and full-line '#' comments."""
    report = ImportReport()
    seen: set[tuple[Scheme, str, int, str]] = set()
    limit_reported = False
    for line_number, line in enumerate(StringIO(text, newline=None), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line_number > MAX_IMPORT_LINES:
            report.skipped += 1
            if not limit_reported:
                report.errors.append(
                    ImportLineError(
                        line=line_number,
                        reason=(
                            f"Import limit of {MAX_IMPORT_LINES} lines exceeded; "
                            "remaining records skipped"
                        ),
                    )
                )
                limit_reported = True
            continue
        try:
            proxy = parse_line(stripped)
        except ProxyParseError as error:
            report.skipped += 1
            report.errors.append(ImportLineError(line=line_number, reason=str(error)))
            continue
        identity = (proxy.scheme, proxy.host, proxy.port, proxy.username)
        if identity in seen:
            report.skipped += 1
            continue
        seen.add(identity)
        report.proxies.append(proxy)
        report.imported += 1
        if proxy.username:
            report.with_auth += 1
        else:
            report.without_auth += 1
    return report
