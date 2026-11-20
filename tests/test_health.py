import asyncio

from app.routes import health


async def _ok() -> None:
    return None


async def _fail() -> None:
    raise RuntimeError("secret connection detail")


async def test_live(client):
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


async def test_ready_all_ok(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", _ok)
    monkeypatch.setattr(health, "check_redis", _ok)
    resp = await client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "ok",
        "checks": {"database": "ok", "redis": "ok"},
    }


async def test_ready_database_down(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", _fail)
    monkeypatch.setattr(health, "check_redis", _ok)
    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"database": "error", "redis": "ok"}


async def test_ready_redis_down(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", _ok)
    monkeypatch.setattr(health, "check_redis", _fail)
    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"] == {"database": "ok", "redis": "error"}


async def test_ready_does_not_leak_error_details(client, monkeypatch):
    monkeypatch.setattr(health, "check_database", _fail)
    monkeypatch.setattr(health, "check_redis", _ok)
    resp = await client.get("/health/ready")
    assert "secret connection detail" not in resp.text


async def test_ready_slow_check_times_out(client, monkeypatch):
    async def slow() -> None:
        await asyncio.sleep(1)

    monkeypatch.setattr(health, "CHECK_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(health, "check_database", slow)
    monkeypatch.setattr(health, "check_redis", _ok)
    resp = await client.get("/health/ready")
    assert resp.status_code == 503
    assert resp.json()["checks"]["database"] == "error"
