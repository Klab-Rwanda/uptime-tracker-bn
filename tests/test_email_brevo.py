import json

import httpx
import pytest

from app.services.email.base import (
    EmailMessage,
    PermanentEmailError,
    RetryableEmailError,
)
from app.services.email.brevo import BrevoEmailProvider

API_KEY = "xkeysib-super-secret"


def make_message(to=None) -> EmailMessage:
    return EmailMessage(
        to=to or ["oncall@acme.com"],
        subject="[DOWN] API is down",
        html="<p>down</p>",
        text="down",
        idempotency_key="inc_1:opened:chan_1",
    )


def make_provider(handler) -> BrevoEmailProvider:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return BrevoEmailProvider(API_KEY, "alerts@example.com", "UpTimeTracker", client=client)


def respond(status: int, body=None, headers=None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body or {}, headers=headers)

    return handler


async def test_send_success_builds_correct_request():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["api_key"] = request.headers["api-key"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json={"messageId": "<abc@relay>"})

    result = await make_provider(handler).send(make_message())

    assert result.provider == "brevo"
    assert result.message_id == "<abc@relay>"
    assert seen["url"] == "https://api.brevo.com/v3/smtp/email"
    assert seen["api_key"] == API_KEY
    assert seen["body"]["sender"] == {"email": "alerts@example.com", "name": "UpTimeTracker"}
    assert seen["body"]["to"] == [{"email": "oncall@acme.com"}]
    assert seen["body"]["subject"] == "[DOWN] API is down"
    assert seen["body"]["htmlContent"] == "<p>down</p>"
    assert seen["body"]["textContent"] == "down"


async def test_success_without_message_id():
    result = await make_provider(respond(201, {})).send(make_message())
    assert result.message_id is None


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_retryable_statuses(status):
    with pytest.raises(RetryableEmailError):
        await make_provider(respond(status)).send(make_message())


async def test_retry_after_header_is_parsed():
    provider = make_provider(respond(429, headers={"retry-after": "30"}))
    with pytest.raises(RetryableEmailError) as info:
        await provider.send(make_message())
    assert info.value.retry_after == 30.0


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_permanent_statuses(status):
    with pytest.raises(PermanentEmailError):
        await make_provider(respond(status, {"message": "nope"})).send(make_message())


async def test_timeout_is_retryable():
    def handler(request):
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(RetryableEmailError):
        await make_provider(handler).send(make_message())


async def test_connection_error_is_retryable():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(RetryableEmailError):
        await make_provider(handler).send(make_message())


async def test_no_recipients_is_permanent():
    empty = EmailMessage(to=[], subject="s", html="h", text="t", idempotency_key="k")
    with pytest.raises(PermanentEmailError):
        await make_provider(respond(201)).send(empty)


async def test_too_many_recipients_is_permanent():
    message = make_message(to=[f"user{i}@acme.com" for i in range(51)])
    with pytest.raises(PermanentEmailError):
        await make_provider(respond(201)).send(message)


async def test_api_key_never_appears_in_errors():
    with pytest.raises(PermanentEmailError) as info:
        await make_provider(respond(401, {"message": "invalid key"})).send(make_message())
    assert API_KEY not in str(info.value)


def test_missing_configuration_is_rejected():
    with pytest.raises(ValueError):
        BrevoEmailProvider("", "alerts@example.com", "X")
    with pytest.raises(ValueError):
        BrevoEmailProvider("key", "", "X")
