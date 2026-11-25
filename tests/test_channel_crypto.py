from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.notification_channels import crypto
from app.notification_channels.crypto import ChannelCipher, ChannelCryptoError

CONFIG = {
    "webhook_url": "https://hooks.acme.com/services/T0/B0/token-abc",
    "secret": "whsec_supersecret",
}


def new_key() -> str:
    return Fernet.generate_key().decode()


def test_roundtrip():
    cipher = ChannelCipher([new_key()])
    assert cipher.decrypt(cipher.encrypt(CONFIG)) == CONFIG


def test_ciphertext_hides_the_secrets():
    token = ChannelCipher([new_key()]).encrypt(CONFIG)
    assert "token-abc" not in token
    assert "whsec_supersecret" not in token
    assert "hooks.acme.com" not in token


def test_same_input_gives_different_ciphertext():
    cipher = ChannelCipher([new_key()])
    assert cipher.encrypt(CONFIG) != cipher.encrypt(CONFIG)


def test_wrong_key_cannot_decrypt():
    token = ChannelCipher([new_key()]).encrypt(CONFIG)
    with pytest.raises(ChannelCryptoError):
        ChannelCipher([new_key()]).decrypt(token)


def test_tampered_token_is_rejected():
    cipher = ChannelCipher([new_key()])
    token = cipher.encrypt(CONFIG)
    flipped = "A" if token[-5] != "A" else "B"
    with pytest.raises(ChannelCryptoError):
        cipher.decrypt(token[:-5] + flipped + token[-4:])


def test_garbage_is_rejected():
    with pytest.raises(ChannelCryptoError):
        ChannelCipher([new_key()]).decrypt("not-a-token")


def test_non_object_payload_is_rejected():
    key = new_key()
    token = Fernet(key.encode()).encrypt(b"[1, 2]").decode()
    with pytest.raises(ChannelCryptoError):
        ChannelCipher([key]).decrypt(token)


def test_old_keys_keep_working_after_rotation():
    old, new = new_key(), new_key()
    token = ChannelCipher([old]).encrypt(CONFIG)

    both = ChannelCipher([new, old])
    assert both.decrypt(token) == CONFIG
    # New data is encrypted with the first key
    assert ChannelCipher([new]).decrypt(both.encrypt(CONFIG)) == CONFIG

    rotated = both.rotate(token)
    assert ChannelCipher([new]).decrypt(rotated) == CONFIG
    with pytest.raises(ChannelCryptoError):
        ChannelCipher([old]).decrypt(rotated)


def test_missing_or_invalid_keys_are_rejected():
    with pytest.raises(ChannelCryptoError):
        ChannelCipher([])
    with pytest.raises(ChannelCryptoError) as info:
        ChannelCipher(["not-a-valid-key"])
    assert "not-a-valid-key" not in str(info.value)


def test_get_cipher_reads_comma_separated_keys(monkeypatch):
    first, second = new_key(), new_key()
    monkeypatch.setattr(
        crypto,
        "get_settings",
        lambda: SimpleNamespace(
            channel_encryption_keys=SecretStr(f" {first} , {second} ")
        ),
    )
    crypto.get_cipher.cache_clear()
    try:
        token = crypto.get_cipher().encrypt(CONFIG)
        assert ChannelCipher([first]).decrypt(token) == CONFIG  # the first key encrypts
        assert ChannelCipher([second, first]).decrypt(token) == CONFIG
    finally:
        crypto.get_cipher.cache_clear()


def test_get_cipher_without_keys_fails(monkeypatch):
    monkeypatch.setattr(
        crypto,
        "get_settings",
        lambda: SimpleNamespace(channel_encryption_keys=SecretStr("")),
    )
    crypto.get_cipher.cache_clear()
    try:
        with pytest.raises(ChannelCryptoError):
            crypto.get_cipher()
    finally:
        crypto.get_cipher.cache_clear()
