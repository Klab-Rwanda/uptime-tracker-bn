import json
import logging

from app.util.logs import JsonFormatter, request_id_ctx


def make_record(msg="hello", **extra):
    record = logging.LogRecord("test", logging.INFO, "f.py", 1, msg, (), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formatter_outputs_valid_json():
    out = json.loads(JsonFormatter().format(make_record("hello")))
    assert out["message"] == "hello"
    assert out["level"] == "INFO"
    assert out["timestamp"].endswith("Z")


def test_formatter_includes_request_id():
    token = request_id_ctx.set("req-123")
    try:
        out = json.loads(JsonFormatter().format(make_record()))
    finally:
        request_id_ctx.reset(token)
    assert out["request_id"] == "req-123"


def test_formatter_includes_extra_fields():
    out = json.loads(JsonFormatter().format(make_record(status=200, path="/x")))
    assert out["status"] == 200
    assert out["path"] == "/x"


def test_formatter_redacts_secrets():
    out = json.loads(
        JsonFormatter().format(make_record(password="hunter2", token="abc"))
    )
    assert out["password"] == "[REDACTED]"
    assert out["token"] == "[REDACTED]"
    assert "hunter2" not in json.dumps(out)


async def test_response_has_generated_request_id(client):
    resp = await client.get("/health/live")
    assert resp.status_code == 200
    assert len(resp.headers["x-request-id"]) > 0


async def test_valid_incoming_request_id_is_kept(client):
    resp = await client.get("/health/live", headers={"X-Request-ID": "abc-123"})
    assert resp.headers["x-request-id"] == "abc-123"


async def test_unsafe_incoming_request_id_is_replaced(client):
    resp = await client.get(
        "/health/live", headers={"X-Request-ID": "bad id with spaces"}
    )
    assert resp.headers["x-request-id"] != "bad id with spaces"
