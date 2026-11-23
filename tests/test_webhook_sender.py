import json
import socket
from datetime import UTC, datetime

import httpx
import pytest

from app.services.webhook.base import (
    PermanentWebhookError,
    RetryableWebhookError,
    WebhookError,
    WebhookEvent,
)
from app.services.webhook.sender import HttpWebhookSender, build_body
from app.services.webhook.signing import verify_signature

SECRET = "whsec_test_secret"
URL = "https://hooks.acme.com/services/secret-token-123"
PUBLIC_IP = "93.184.216.34"
NOW = 1_700_000_000

EVENT = WebhookEvent(
    event_type="incident.opened",
    delivery_id="inc_1:opened:chan_1",
    created_at=datetime(2026, 11, 20, 10, 0, tzinfo=UTC),
    data={"incident_id": "inc_1", "monitor_name": "Customer API"},
)


async def public_resolver(host, port):
    return [PUBLIC_IP]


def make_sender(handler, resolver=public_resolver, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpWebhookSender(
        client=client, resolver=resolver, clock=lambda: float(NOW), **kwargs
    )


def respond(status, headers=None):
    def handler(request):
        return httpx.Response(status, headers=headers)

    return handler


async def test_successful_delivery():
    seen = {}

    def handler(request):
        seen["request"] = request
        seen["body"] = request.content
        return httpx.Response(204)

    result = await make_sender(handler).send(URL, SECRET, EVENT)
    request, body = seen["request"], seen["body"]

    assert result.status_code == 204
    assert request.method == "POST"
    # We connect to the checked IP, but keep the real host for Host and TLS
    assert request.url.host == PUBLIC_IP
    assert request.url.path == "/services/secret-token-123"
    assert request.headers["host"] == "hooks.acme.com"
    assert request.extensions["sni_hostname"] == "hooks.acme.com"

    assert request.headers["content-type"] == "application/json"
    assert request.headers["x-uptime-event"] == "incident.opened"
    assert request.headers["x-uptime-delivery"] == EVENT.delivery_id
    assert request.headers["x-uptime-timestamp"] == str(NOW)
    assert verify_signature(
        SECRET,
        request.headers["x-uptime-timestamp"],
        request.headers["x-uptime-signature"],
        body,
        now=NOW,
    )
    assert json.loads(body) == {
        "id": EVENT.delivery_id,
        "type": "incident.opened",
        "created_at": "2026-11-20T10:00:00Z",
        "data": EVENT.data,
    }


async def test_secret_is_never_sent():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200)

    await make_sender(handler).send(URL, SECRET, EVENT)
    request = seen["request"]
    assert SECRET not in request.content.decode()
    assert SECRET not in " ".join(request.headers.values())


def test_body_is_compact_and_sorted():
    expected = json.dumps(
        {
            "id": EVENT.delivery_id,
            "type": "incident.opened",
            "created_at": "2026-11-20T10:00:00Z",
            "data": EVENT.data,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert build_body(EVENT) == expected


def test_naive_datetime_is_treated_as_utc():
    naive = WebhookEvent("x", "d", datetime(2026, 11, 20, 10, 0), {})
    assert json.loads(build_body(naive))["created_at"] == "2026-11-20T10:00:00Z"


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
async def test_retryable_statuses(status):
    with pytest.raises(RetryableWebhookError):
        await make_sender(respond(status)).send(URL, SECRET, EVENT)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
async def test_permanent_statuses(status):
    with pytest.raises(PermanentWebhookError):
        await make_sender(respond(status)).send(URL, SECRET, EVENT)


async def test_retry_after_is_parsed():
    sender = make_sender(respond(429, headers={"retry-after": "30"}))
    with pytest.raises(RetryableWebhookError) as info:
        await sender.send(URL, SECRET, EVENT)
    assert info.value.retry_after == 30.0


async def test_redirects_are_not_followed():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
        )

    with pytest.raises(PermanentWebhookError):
        await make_sender(handler).send(URL, SECRET, EVENT)
    assert len(calls) == 1


async def test_private_target_is_blocked_before_any_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    async def private_resolver(host, port):
        return ["10.0.0.5"]

    with pytest.raises(PermanentWebhookError) as info:
        await make_sender(handler, resolver=private_resolver).send(URL, SECRET, EVENT)
    assert calls == []
    assert "10.0.0.5" not in str(info.value)


async def test_http_urls_are_blocked_by_default():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200)

    with pytest.raises(PermanentWebhookError):
        await make_sender(handler).send("http://hooks.acme.com/x", SECRET, EVENT)
    assert calls == []


async def test_dns_failure_is_retryable():
    async def failing_resolver(host, port):
        raise socket.gaierror("name resolution failed")

    with pytest.raises(RetryableWebhookError):
        await make_sender(respond(200), resolver=failing_resolver).send(URL, SECRET, EVENT)


async def test_dns_timeout_is_retryable():
    async def slow_resolver(host, port):
        raise TimeoutError

    with pytest.raises(RetryableWebhookError):
        await make_sender(respond(200), resolver=slow_resolver).send(URL, SECRET, EVENT)


async def test_request_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(RetryableWebhookError):
        await make_sender(handler).send(URL, SECRET, EVENT)


async def test_connection_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(RetryableWebhookError):
        await make_sender(handler).send(URL, SECRET, EVENT)


async def dual_stack_resolver(host, port):
    return ["2606:4700::1", PUBLIC_IP]


async def test_falls_back_to_next_ip_when_connect_fails():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        if request.url.host == "2606:4700::1":
            raise httpx.ConnectError("no IPv6 route", request=request)
        return httpx.Response(200)

    result = await make_sender(handler, resolver=dual_stack_resolver).send(URL, SECRET, EVENT)
    assert result.status_code == 200
    assert hosts == ["2606:4700::1", PUBLIC_IP]


async def test_no_fallback_after_request_was_sent():
    # A read timeout means the endpoint may already have the webhook: don't send twice
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RetryableWebhookError):
        await make_sender(handler, resolver=dual_stack_resolver).send(URL, SECRET, EVENT)
    assert hosts == ["2606:4700::1"]


async def test_all_ips_unreachable_is_retryable():
    hosts = []

    def handler(request):
        hosts.append(request.url.host)
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(RetryableWebhookError):
        await make_sender(handler, resolver=dual_stack_resolver).send(URL, SECRET, EVENT)
    assert hosts == ["2606:4700::1", PUBLIC_IP]


async def test_url_token_never_appears_in_errors():
    async def private_resolver(host, port):
        return ["10.0.0.5"]

    cases = [
        make_sender(respond(500)),
        make_sender(respond(400)),
        make_sender(respond(302)),
        make_sender(respond(200), resolver=private_resolver),
    ]
    for sender in cases:
        with pytest.raises(WebhookError) as info:
            await sender.send(URL, SECRET, EVENT)
        assert "secret-token-123" not in str(info.value)


async def test_empty_secret_is_rejected():
    with pytest.raises(ValueError):
        await make_sender(respond(200)).send(URL, "", EVENT)


async def test_private_mode_allows_local_http_targets():
    seen = {}

    def handler(request):
        seen["request"] = request
        return httpx.Response(200)

    async def local_resolver(host, port):
        return ["127.0.0.1"]

    sender = make_sender(handler, resolver=local_resolver, allow_private=True)
    await sender.send("http://localhost:9000/hook", SECRET, EVENT)

    request = seen["request"]
    assert request.url.host == "127.0.0.1"
    assert request.url.port == 9000
    assert request.headers["host"] == "localhost:9000"
    assert request.extensions.get("sni_hostname") is None
