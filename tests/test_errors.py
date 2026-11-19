import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, HttpUrl

from app.util.errors import AppError, register_error_handlers


def build_test_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    class Item(BaseModel):
        target: HttpUrl

    @app.post("/items")
    async def create_item(item: Item) -> dict[str, bool]:
        return {"ok": True}

    @app.get("/raise/{code}")
    async def raise_http(code: int) -> None:
        raise HTTPException(status_code=code, detail="Something happened")

    @app.get("/conflict")
    async def conflict() -> None:
        raise AppError(409, "conflict", "Slug already taken")

    @app.get("/boom")
    async def boom() -> None:
        raise RuntimeError("secret internal detail")

    return app


@pytest.fixture
async def err_client():
    transport = ASGITransport(app=build_test_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def test_validation_error_format(err_client):
    resp = await err_client.post("/items", json={"target": "not-a-url"})
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Request validation failed"
    assert error["details"][0]["field"] == "target"
    assert error["details"][0]["message"]


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (400, "bad_request"),
        (401, "unauthenticated"),
        (403, "forbidden"),
        (404, "not_found"),
        (409, "conflict"),
        (429, "too_many_requests"),
    ],
)
async def test_http_errors_use_standard_format(err_client, status, code):
    resp = await err_client.get(f"/raise/{status}")
    assert resp.status_code == status
    assert resp.json() == {
        "error": {"code": code, "message": "Something happened"}
    }


async def test_app_error(err_client):
    resp = await err_client.get("/conflict")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "conflict"
    assert resp.json()["error"]["message"] == "Slug already taken"


async def test_unknown_route_returns_not_found(err_client):
    resp = await err_client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_unhandled_error_hides_internal_details(err_client):
    resp = await err_client.get("/boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "secret internal detail" not in resp.text


async def test_real_app_uses_standard_404(client):
    resp = await client.get("/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
