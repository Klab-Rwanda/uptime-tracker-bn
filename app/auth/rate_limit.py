import hashlib
import hmac

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.auth.errors import APIError
from app.util.config import get_settings

_INCREMENT_WITH_EXPIRY = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
    redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


async def enforce_login_rate_limit(
    redis: Redis,
    *,
    email: str,
    client_ip: str,
) -> None:
    settings = get_settings()
    identity = f"{client_ip}:{email}".encode()
    key_digest = hmac.new(
        settings.jwt_secret.get_secret_value().encode("utf-8"),
        identity,
        hashlib.sha256,
    ).hexdigest()
    try:
        attempts = await redis.eval(
            _INCREMENT_WITH_EXPIRY,
            1,
            f"auth:login:{key_digest}",
            900,
        )
    except RedisError as error:
        raise APIError(
            503,
            "authentication_unavailable",
            "Authentication is temporarily unavailable",
        ) from error

    if attempts > 10:
        raise APIError(
            429,
            "rate_limit_exceeded",
            "Too many login attempts. Try again later.",
        )
