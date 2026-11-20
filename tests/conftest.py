import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.fixture
async def client():
    """Calls the FastAPI app directly, with no network."""
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def auth_headers():
    """TODO: replace with a real login once Developer A's auth exists."""
    return {"Authorization": "Bearer TODO"}


@pytest.fixture
def outsider_headers():
    """TODO: a user from a DIFFERENT organization, for isolation tests."""
    return {"Authorization": "Bearer TODO-OUTSIDER"}