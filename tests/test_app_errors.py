"""Guards app-wide behaviour that a merge once broke."""

from uuid import uuid4

import pytest
from fastapi import APIRouter

from app.auth.errors import APIError
from app.main import app


async def test_api_errors_keep_their_status_not_500(client):
    # A merge once dropped the APIError handler: every 401/403/404 became a 500
    resp = await client.get(f"/v1/monitors?organization_id={uuid4()}")
    assert resp.status_code == 401
    assert resp.json() == {
        "error": {"code": "unauthenticated", "message": "Authentication is required"}
    }
    assert resp.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize("status_code", [400, 403, 404, 409, 422, 429])
async def test_any_api_error_status_is_kept(client, status_code):
    router = APIRouter()

    @router.get(f"/__test_api_error_{status_code}")
    async def boom() -> None:
        raise APIError(status_code, "some_code", "Some message")

    app.include_router(router)
    try:
        resp = await client.get(f"/__test_api_error_{status_code}")
    finally:
        app.router.routes[:] = [
            r for r in app.router.routes if getattr(r, "path", "") != f"/__test_api_error_{status_code}"
        ]
    assert resp.status_code == status_code
    assert resp.json() == {"error": {"code": "some_code", "message": "Some message"}}
