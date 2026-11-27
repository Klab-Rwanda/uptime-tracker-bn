"""Which frontend addresses may call the API from a browser (CORS).

Browsers block a page on one address (e.g. https://app.example.com) from
reading responses of another (our API) unless the API says that page is
allowed. Only the addresses in CORS_ORIGINS are. Logins use Bearer tokens,
not cookies, so credentials stay off.
"""

from urllib.parse import urlsplit

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.util.config import get_settings

ALLOWED_METHODS = ["GET", "POST", "PATCH", "PUT", "DELETE", "OPTIONS"]
ALLOWED_HEADERS = ["Authorization", "Content-Type", "Accept", "X-Request-ID"]
# Response headers the frontend's JavaScript may read
EXPOSED_HEADERS = ["X-Request-ID", "Retry-After", "X-Cache"]
PREFLIGHT_CACHE_SECONDS = 600


def parse_origins(raw: str) -> list[str]:
    """Turn "http://localhost:3000, https://app.example.com/" into a clean list.

    Raises ValueError for anything that is not a plain origin, so a typo stops
    the app at startup instead of silently blocking (or allowing) browsers.
    """
    origins: list[str] = []
    for item in raw.split(","):
        origin = item.strip().rstrip("/")
        if not origin:
            continue
        if origin == "*":
            raise ValueError(
                "CORS_ORIGINS must list the frontend addresses; '*' would let any website call the API"
            )
        parts = urlsplit(origin)
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.path
            or parts.query
            or parts.fragment
            or parts.username
            or parts.password
        ):
            raise ValueError(
                f"Invalid CORS origin {origin!r}: use scheme://host[:port], "
                "e.g. https://app.example.com"
            )
        origin = f"{parts.scheme}://{parts.netloc}".lower()
        if origin not in origins:
            origins.append(origin)
    return origins


def register_cors(app: FastAPI, origins: list[str] | None = None) -> None:
    """Allow browser calls from `origins` (default: the CORS_ORIGINS setting).
    Register it after the other middleware so it wraps them."""
    allowed = parse_origins(get_settings().cors_origins) if origins is None else origins
    if not allowed:
        return
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed,
        allow_credentials=False,
        allow_methods=ALLOWED_METHODS,
        allow_headers=ALLOWED_HEADERS,
        expose_headers=EXPOSED_HEADERS,
        max_age=PREFLIGHT_CACHE_SECONDS,
    )
