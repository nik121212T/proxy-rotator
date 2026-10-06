"""Fernet interoperability and credential protection."""

import base64
import traceback

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.crypto import CryptoConfigError, decrypt_password, derive_key, encrypt_password


def test_derive_key_matches_pbkdf2_sha256_480000_reference() -> None:
    # Independently calculated with stdlib hashlib.pbkdf2_hmac, dklen=32.
    key = derive_key(SecretStr("synthetic-master-secret"), bytes(range(16)))

    assert key == b"B1kcPZMIj3d0lbTcsOo36Mg_sSTtbuDiNH02WorO_2w="


def test_encrypt_password_is_randomized_and_fernet_compatible() -> None:
    key = derive_key(SecretStr("synthetic-master-secret"), bytes(range(16)))
    password = SecretStr("synthetic-秘密-password")

    first = encrypt_password(password, key)
    second = encrypt_password(password, key)

    assert first != second
    assert password.get_secret_value().encode() not in first
    assert Fernet(key).decrypt(first).decode() == password.get_secret_value()
    assert decrypt_password(first, key) == password
    assert password.get_secret_value() not in repr(decrypt_password(first, key))


@pytest.mark.parametrize("secret", ["", "   "])
def test_missing_master_secret_is_configuration_error(secret: str) -> None:
    with pytest.raises(CryptoConfigError) as error:
        derive_key(SecretStr(secret), bytes(range(16)))

    assert error.value.exit_code == 78
    assert "MASTER_SECRET" in str(error.value)


def test_derivation_rejects_short_salt() -> None:
    with pytest.raises(CryptoConfigError):
        derive_key(SecretStr("synthetic-master-secret"), b"short")


def test_wrong_secret_and_tampered_token_fail_closed_without_secrets() -> None:
    secret = SecretStr("synthetic-master-secret")
    key = derive_key(secret, bytes(range(16)))
    other_key = derive_key(SecretStr("different-synthetic-secret"), bytes(range(16)))
    password = SecretStr("synthetic-sensitive-password")
    token = encrypt_password(password, key)
    payload = bytearray(base64.urlsafe_b64decode(token))
    payload[-1] ^= 1
    tampered = base64.urlsafe_b64encode(payload)

    for encrypted, candidate in [(token, other_key), (tampered, key), (b"invalid", key)]:
        with pytest.raises(CryptoConfigError) as error:
            decrypt_password(encrypted, candidate)

        assert error.value.exit_code == 78
        rendered = "".join(traceback.format_exception(error.value))
        assert secret.get_secret_value() not in rendered
        assert password.get_secret_value() not in rendered
        assert token.decode() not in rendered


def test_invalid_fernet_key_is_configuration_error() -> None:
    with pytest.raises(CryptoConfigError):
        encrypt_password(SecretStr("synthetic-password"), b"invalid")
    with pytest.raises(CryptoConfigError):
        decrypt_password(b"invalid", b"invalid")
