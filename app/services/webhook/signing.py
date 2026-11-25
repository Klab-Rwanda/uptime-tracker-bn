import hashlib
import hmac
import secrets
import time

SIGNATURE_PREFIX = "sha256="


def generate_secret() -> str:
    """A new signing secret. Show it to the customer once, store it encrypted."""
    return "whsec_" + secrets.token_urlsafe(32)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """Signature over "<timestamp>.<body>" so a body can't be replayed later."""
    message = str(timestamp).encode() + b"." + body
    digest = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return SIGNATURE_PREFIX + digest


def verify_signature(
    secret: str,
    timestamp_header: str | None,
    signature_header: str | None,
    body: bytes,
    *,
    tolerance_seconds: int = 300,
    now: float | None = None,
) -> bool:
    """What a receiver does. Used in our tests and in the customer docs."""
    try:
        timestamp = int(timestamp_header or "")
    except ValueError:
        return False
    current = time.time() if now is None else now
    if abs(current - timestamp) > tolerance_seconds:
        return False
    expected = sign(secret, timestamp, body)
    return hmac.compare_digest(expected.encode(), (signature_header or "").encode())
