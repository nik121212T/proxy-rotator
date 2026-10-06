"""Persist the derivation salt separately from the pure crypto module."""

import os
import stat
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.crypto import CryptoConfigError, decrypt_password, derive_key, encrypt_password
from app.salt import load_or_create_salt


def test_salt_survives_restart_and_keeps_credentials_decryptable(tmp_path: Path) -> None:
    path = tmp_path / "data" / ".salt"
    secret = SecretStr("synthetic-master-secret")
    salt = load_or_create_salt(path)
    token = encrypt_password(SecretStr("synthetic-password"), derive_key(secret, salt))

    loaded = load_or_create_salt(path)

    assert len(salt) == 16
    assert salt == loaded == path.read_bytes()
    assert decrypt_password(token, derive_key(secret, loaded)).get_secret_value() == (
        "synthetic-password"
    )
    assert secret.get_secret_value().encode() not in path.read_bytes()
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("contents", [b"", b"short", b"x" * 17])
def test_invalid_existing_salt_is_not_silently_replaced(tmp_path: Path, contents: bytes) -> None:
    path = tmp_path / ".salt"
    path.write_bytes(contents)

    with pytest.raises(CryptoConfigError) as error:
        load_or_create_salt(path)

    assert error.value.exit_code == 78
    assert path.read_bytes() == contents


def test_salt_path_error_becomes_safe_configuration_error(tmp_path: Path) -> None:
    path = tmp_path / "directory"
    path.mkdir()

    with pytest.raises(CryptoConfigError):
        load_or_create_salt(path)


@pytest.mark.skipif(os.name != "posix", reason="Windows permissions use inherited ACLs")
def test_existing_salt_permissions_are_restricted(tmp_path: Path) -> None:
    path = tmp_path / ".salt"
    path.write_bytes(bytes(range(16)))
    path.chmod(0o644)

    assert load_or_create_salt(path) == bytes(range(16))
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
