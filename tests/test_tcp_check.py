import asyncio
import pytest

from app.checks.tcp_check import TCPCheck


def make_check(host="example.com", port=443, timeout_seconds=5):
    return TCPCheck(monitor_id="m1", host=host, port=port, timeout_seconds=timeout_seconds)


# --- helpers ------------------------------------------------------------------


def open_connection_ok(host, port):
    """Simulates a successful TCP handshake."""
    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    writer = asyncio.StreamWriter(None, protocol, reader, asyncio.get_event_loop())  # type: ignore[arg-type]
    future: asyncio.Future = asyncio.get_event_loop().create_future()
    future.set_result((reader, writer))
    return future


# --- results ------------------------------------------------------------------


async def test_up_when_connection_succeeds(monkeypatch):
    async def fake_open(host, port):
        reader = asyncio.StreamReader()
        # A minimal writer-like object is enough; we only call close/wait_closed
        class FakeWriter:
            def close(self): pass
            async def wait_closed(self): pass

        return reader, FakeWriter()

    monkeypatch.setattr("asyncio.open_connection", fake_open)
    result = await make_check().run()
    assert result.status == "up"
    assert result.error_message is None
    assert result.checked_at.tzinfo is not None


async def test_down_on_connection_refused(monkeypatch):
    async def fake_open(host, port):
        raise OSError("Connection refused")

    monkeypatch.setattr("asyncio.open_connection", fake_open)
    result = await make_check().run()
    assert result.status == "down"
    assert result.error_message == "Connection failed (OSError)"
    # Raw OS message must not leak (could contain internal IPs)
    assert "Connection refused" not in result.error_message


async def test_down_on_timeout(monkeypatch):
    async def fake_open(host, port):
        raise TimeoutError

    monkeypatch.setattr("asyncio.open_connection", fake_open)
    result = await make_check(timeout_seconds=3).run()
    assert result.status == "down"
    assert result.error_message == "Connection timed out after 3s"


async def test_response_time_is_recorded(monkeypatch):
    async def fake_open(host, port):
        class FakeWriter:
            def close(self): pass
            async def wait_closed(self): pass

        return asyncio.StreamReader(), FakeWriter()

    monkeypatch.setattr("asyncio.open_connection", fake_open)
    result = await make_check().run()
    assert result.response_time_ms >= 0


async def test_host_and_port_are_passed_correctly(monkeypatch):
    calls = {}

    async def fake_open(host, port):
        calls["host"] = host
        calls["port"] = port

        class FakeWriter:
            def close(self): pass
            async def wait_closed(self): pass

        return asyncio.StreamReader(), FakeWriter()

    monkeypatch.setattr("asyncio.open_connection", fake_open)
    await TCPCheck(monitor_id="m", host="db.acme.com", port=5432).run()
    assert calls["host"] == "db.acme.com"
    assert calls["port"] == 5432
