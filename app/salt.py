"""Storage of the application's public derivation salt, never its master secret."""

import os
import secrets
from pathlib import Path

from app.crypto import CryptoConfigError


def load_or_create_salt(path: Path = Path("data") / ".salt") -> bytes:
    """Create the salt once with owner-only POSIX permissions, or reuse it."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with path.open("rb") as stream:
                salt = stream.read(17)
            if len(salt) != 16:
                raise CryptoConfigError(
                    "Saved salt is invalid; restore it instead of replacing it"
                ) from None
            if os.name == "posix":
                path.chmod(0o600)
            return salt
        with os.fdopen(descriptor, "wb") as stream:
            salt = secrets.token_bytes(16)
            stream.write(salt)
        return salt
    except OSError:
        raise CryptoConfigError("Cannot access the saved derivation salt") from None
