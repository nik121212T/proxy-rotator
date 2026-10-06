"""Credential encryption primitives; no environment or filesystem access."""

import base64

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from pydantic import SecretStr


class CryptoConfigError(ValueError):
    """Safe startup error; the entry point translates this to EX_CONFIG."""

    exit_code = 78


def derive_key(master_secret: SecretStr, salt: bytes) -> bytes:
    """Derive a Fernet key using the persisted application salt."""
    if not master_secret.get_secret_value().strip():
        raise CryptoConfigError("MASTER_SECRET is required")
    if len(salt) < 16:
        raise CryptoConfigError("The derivation salt must contain at least 16 bytes")
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=480_000)
    return base64.urlsafe_b64encode(kdf.derive(master_secret.get_secret_value().encode("utf-8")))


def encrypt_password(password: SecretStr, key: bytes) -> bytes:
    """Return an authenticated Fernet token suitable for password_enc."""
    try:
        return Fernet(key).encrypt(password.get_secret_value().encode("utf-8"))
    except ValueError:
        raise CryptoConfigError("Invalid encryption key") from None


def decrypt_password(password_enc: bytes, key: bytes) -> SecretStr:
    """Decrypt into a secret wrapper that masks accidental representations."""
    try:
        return SecretStr(Fernet(key).decrypt(password_enc).decode("utf-8"))
    except (InvalidToken, ValueError):
        raise CryptoConfigError(
            "Cannot decrypt stored credentials; verify MASTER_SECRET and the saved salt"
        ) from None
