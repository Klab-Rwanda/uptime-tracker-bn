import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.auth.errors import APIError
from app.main import handle_api_error
from app.util.config import get_settings
from app.util.cors import parse_origins, register_cors

FRONTEND = "http://localhost:3000"


# --- reading the setting -----------------------------------------------------------


def test_parse_origins_cleans_the_list():
    raw = " http://localhost:3000/ , https://App.Example.com,,http://localhost:3000 "
    assert parse_origins(raw) == ["http://localhost:3000", "https://app.example.com"]


def test_empty_setting_allows_nothing():
    assert parse_origins("") == []
    assert parse_origins(" , ") == []


@pytest.mark.parametrize(
    "raw",
    [
        "*",
        "localhost:3000",  # no scheme
        "ftp://example.com",
        "https://example.com/app",  # a path is not an origin
        "https://example.com?x=1",
        "https://user:pass@example.com",
        "https://",
    ],
)
def test_invalid_origins_are_refused(raw):
    with pytest.raises(ValueError):
        parse_origins(raw)


# --- what browsers see -----------------------------------------------------------------


def make_app(origins: list[str] | None) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(APIError, handle_api_error)  # type: ignore[arg-type]

    @app.get("/thing")
    async def thing() -> dict[str, str]:
        return {"ok": "yes"}

    @app.get("/missing")
    async def missing() -> None:
        raise APIError(404, "not_found", "Nope")

    register_cors(app, origins)
    return app


async def call(app: FastAPI, method: str, path: str, headers: dict[str, str]):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://api.test") as client:
        return await client.request(method, path, headers=headers)


def preflight(origin: str) -> dict[str, str]:
    return {
        "Origin": origin,
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type",
    }


async def test_allowed_frontend_passes_the_preflight():
    resp = await call(make_app([FRONTEND]), "OPTIONS", "/thing", preflight(FRONTEND))
    assert resp.status_code == 200
    assert resp.headers["access-control-allow-origin"] == FRONTEND
    assert "POST" in resp.headers["access-control-allow-methods"]
    assert "authorization" in resp.headers["access-control-allow-headers"].lower()
    assert resp.headers["access-control-max-age"] == "600"
    assert "access-control-allow-credentials" not in resp.headers  # Bearer tokens, no cookies


async def test_other_websites_are_refused():
    app = make_app([FRONTEND])
    resp = await call(app, "OPTIONS", "/thing", preflight("https://evil.example"))
    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers

    resp = await call(app, "GET", "/thing", {"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in resp.headers  # the browser hides the answer


async def test_allowed_frontend_can_read_answers_and_headers():
    resp = await call(make_app([FRONTEND]), "GET", "/thing", {"Origin": FRONTEND})
    assert resp.json() == {"ok": "yes"}
    assert resp.headers["access-control-allow-origin"] == FRONTEND
    exposed = resp.headers["access-control-expose-headers"].lower()
    assert {"x-request-id", "retry-after", "x-cache"} <= {h.strip() for h in exposed.split(",")}


async def test_error_answers_carry_the_headers_too():
    # Otherwise the frontend sees a vague "CORS error" instead of the real 404
    resp = await call(make_app([FRONTEND]), "GET", "/missing", {"Origin": FRONTEND})
    assert resp.status_code == 404
    assert resp.headers["access-control-allow-origin"] == FRONTEND


async def test_without_origins_nothing_is_added():
    resp = await call(make_app([]), "GET", "/thing", {"Origin": FRONTEND})
    assert "access-control-allow-origin" not in resp.headers


async def test_origins_come_from_the_setting(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", f"{FRONTEND}, https://app.example.com")
    get_settings.cache_clear()
    try:
        resp = await call(make_app(None), "GET", "/thing", {"Origin": "https://app.example.com"})
        assert resp.headers["access-control-allow-origin"] == "https://app.example.com"
    finally:
        get_settings.cache_clear()


def test_a_bad_setting_stops_startup(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "*")
    get_settings.cache_clear()
    try:
        with pytest.raises(ValueError):
            make_app(None)
    finally:
        get_settings.cache_clear()
