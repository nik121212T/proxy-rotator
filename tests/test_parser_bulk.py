"""Batch import behavior, including safe errors and resource limits."""

from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from app.parser import parse_bulk, parse_line


def test_bulk_import_retains_valid_records_and_ignores_empty_comments() -> None:
    text = "\r\n # synthetic proxies\r\nuser:pw@proxy.example:80\r\n\t\n"

    report = parse_bulk(text)

    assert report.imported == 1
    assert report.skipped == 0
    assert report.errors == []
    assert len(report.proxies) == 1
    assert report.proxies[0].host == "proxy.example"
    assert report.model_dump() == {"imported": 1, "with_auth": 1, "without_auth": 0, "skipped": 0, "errors": []}


def test_bulk_import_is_partial_and_deduplicates_by_identity() -> None:
    text = "\n".join(
        [
            "# comment",
            "user:first@PROXY.example:80",
            "http://user:second@proxy.example:00080",
            "user:synthetic-sensitive-value@proxy.example:invalid",
            "https://user:pw@proxy.example:80",
            "user:pw@proxy.example:81",
            "other:pw@proxy.example:80",
            "user:pw@other.example:80",
            "user:p# /%?[]\\w@[2001:DB8::1]:80",
            "user:changed@[2001:db8:0:0:0:0:0:1]:80",
        ]
    )

    report = parse_bulk(text)

    assert report.imported == 6
    assert report.skipped == 3
    assert report.proxies[0].password.get_secret_value() == "first"
    assert report.proxies[-1].password.get_secret_value() == "p# /%?[]\\w"
    assert [error.line for error in report.errors] == [4]
    assert report.errors[0].reason == "Invalid proxy format"
    assert "synthetic-sensitive-value" not in report.model_dump_json()
    assert "synthetic-sensitive-value" not in repr(report)


def test_bulk_import_caps_work_at_2000_physical_lines() -> None:
    lines = [f"user:pw@proxy.example:{port}" for port in range(1, 2004)]
    with patch("app.parser.parse_line", wraps=parse_line) as parse:
        report = parse_bulk("\n".join(lines))

    assert parse.call_count == 2000
    assert report.imported == 2000
    assert report.skipped == 3
    assert report.model_dump()["errors"] == [
        {"line": 2001, "reason": "Import limit of 2000 lines exceeded; remaining records skipped"}
    ]


def test_bulk_import_counts_blank_and_comment_lines_toward_limit() -> None:
    report = parse_bulk("\n# comment\n" * 1000 + "user:pw@proxy.example:80\n")

    assert report.imported == 0
    assert report.skipped == 1
    assert report.errors[0].line == 2001


@st.composite
def mixed_imports(draw: st.DrawFn) -> tuple[str, int, int, list[int]]:
    ports = draw(st.lists(st.integers(1, 65535), min_size=1, max_size=20, unique=True))
    records: list[tuple[str, str]] = [("# comment", "ignore"), ("", "ignore")]
    for port in ports:
        records.extend(
            [
                (f"user:pw@PROXY.example:{port}", "valid"),
                (f"http://user:changed@proxy.example:{port}", "valid"),
                (f"user:synthetic-sensitive-value@proxy.example:bad{port}", "invalid"),
            ]
        )
    shuffled = draw(st.permutations(records))
    invalid_lines = [i for i, (_, kind) in enumerate(shuffled, 1) if kind == "invalid"]
    return "\n".join(line for line, _ in shuffled), len(ports), len(ports) * 2, invalid_lines


@settings(max_examples=200, derandomize=True, database=None)
@given(batch=mixed_imports())
def test_bulk_import_mixed_property(batch: tuple[str, int, int, list[int]]) -> None:
    text, imported, skipped, invalid_lines = batch

    report = parse_bulk(text)

    assert report.imported == imported
    assert len(report.proxies) == imported
    assert report.skipped == skipped
    assert [error.line for error in report.errors] == invalid_lines
    assert "synthetic-sensitive-value" not in report.model_dump_json()


@settings(max_examples=200, derandomize=True, database=None)
@given(text=st.text(max_size=5000))
def test_bulk_import_never_raises_on_arbitrary_text(text: str) -> None:
    report = parse_bulk(text)

    assert report.imported == len(report.proxies)
    assert report.skipped >= len(report.errors)
