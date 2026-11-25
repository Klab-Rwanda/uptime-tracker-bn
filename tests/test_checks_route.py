from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from uuid import uuid4

import pytest

from app.auth.dependencies import AuthenticatedUser, get_current_auth
from app.checks.result import CheckResult
from app.main import app
from app.routes import checks


@pytest.fixture
def logged_in():
    user = SimpleNamespace(id=uuid4(), email="u@example.com")
    app.dependency_overrides[get_current_auth] = lambda: AuthenticatedUser(user=user, session_id=uuid4())
    yield
    app.dependency_overrides.pop(get_current_auth, None)


class FakeCheck:
    instances: ClassVar[list["FakeCheck"]] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeCheck.instances.append(self)

    async def run(self):
        return CheckResult(
            monitor_id="adhoc", status="up", response_time_ms=12, checked_at=datetime.now(UTC)
        )


async def test_requires_login(client):
    resp = await client.post("/checks/run", json={"url": "https://status.acme.com/"})
    assert resp.status_code == 401


async def test_logged_in_user_can_run_a_check(client, logged_in, monkeypatch):
    FakeCheck.instances.clear()
    monkeypatch.setattr(checks, "HTTPCheck", FakeCheck)
    monkeypatch.setattr(
        checks, "get_settings", lambda: SimpleNamespace(monitor_allow_private_targets=False)
    )
    resp = await client.post("/checks/run", json={"url": "https://status.acme.com/"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "up"
    # The real check gets the SSRF protection (private targets refused)
    assert FakeCheck.instances[0].kwargs["allow_private"] is False


async def test_internal_targets_are_refused(client, logged_in):
    resp = await client.post(
        "/checks/run", json={"url": "http://169.254.169.254/latest/meta-data/", "timeout_seconds": 2}
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "down"
    assert resp.json()["error_message"].startswith("Target is not allowed")


@pytest.mark.parametrize(
    "body",
    [
        {"url": "https://status.acme.com/", "method": "POST"},
        {"url": "https://status.acme.com/", "method": "DELETE"},
        {"url": "https://status.acme.com/", "timeout_seconds": 0},
        {"url": "https://status.acme.com/", "timeout_seconds": 3600},
        {"url": "https://status.acme.com/", "expected_status_codes": []},
        {"url": "https://status.acme.com/", "expected_status_codes": [42]},
        {"url": "not a url"},
    ],
)
async def test_bad_requests_are_rejected(client, logged_in, body):
    resp = await client.post("/checks/run", json=body)
    assert resp.status_code == 422
