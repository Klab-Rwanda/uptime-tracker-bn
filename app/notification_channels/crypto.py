import json
from functools import lru_cache
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

from app.util.config import get_settings


class ChannelCryptoError(Exception):
    """Encrypting or decrypting a channel configuration failed.

    Messages never contain key material or configuration values.
    """


class ChannelCipher:
    """Encrypts channel configurations. The first key encrypts, all keys decrypt."""

    def __init__(self, keys: list[str]) -> None:
        if not keys:
            raise ChannelCryptoError("CHANNEL_ENCRYPTION_KEYS is not configured")
        try:
            self._fernet = MultiFernet([Fernet(key.encode()) for key in keys])
        except ValueError as exc:
            raise ChannelCryptoError(
                "CHANNEL_ENCRYPTION_KEYS contains an invalid key"
            ) from exc

    def encrypt(self, config: dict[str, Any]) -> str:
        payload = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
        return self._fernet.encrypt(payload).decode()

    def decrypt(self, token: str) -> dict[str, Any]:
        try:
            data = json.loads(self._fernet.decrypt(token.encode()))
        except (InvalidToken, ValueError) as exc:
            raise ChannelCryptoError(
                "Stored channel configuration cannot be decrypted"
            ) from exc
        if not isinstance(data, dict):
            raise ChannelCryptoError("Stored channel configuration is malformed")
        return data

    def rotate(self, token: str) -> str:
        """Re-encrypt a token with the primary (first) key."""
        try:
            return self._fernet.rotate(token.encode()).decode()
        except InvalidToken as exc:
            raise ChannelCryptoError(
                "Stored channel configuration cannot be decrypted"
            ) from exc


@lru_cache
def get_cipher() -> ChannelCipher:
    raw = get_settings().channel_encryption_keys.get_secret_value()
    keys = [part.strip() for part in raw.split(",") if part.strip()]
    return ChannelCipher(keys)
