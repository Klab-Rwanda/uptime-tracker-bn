import json
from datetime import UTC, datetime

import httpx
import pytest

from app.services.notifications.retry import FailureKind, classify_failure
from app.services.slack.messages import channel_test_message, escape, incident_message
from app.services.slack.sender import (
    HttpSlackSender,
    PermanentSlackError,
    RetryableSlackError,
    check_slack_url,
)

URL = "https://hooks.slack.com/services/T0001/B0002/abcdefSECRET123"


# --- which URLs are accepted ---------------------------------------------------------


def test_a_slack_webhook_url_is_accepted():
    assert check_slack_url(f"  {URL}  ") == URL


@pytest.mark.parametrize(
    "url",
    [
        "http://hooks.slack.com/services/T0001/B0002/abc",  # not https
        "https://hooks.slack.com.evil.com/services/T0001/B0002/abc",
        "https://evil.com/services/T0001/B0002/abc",
        "https://hooks.slack.com:8443/services/T0001/B0002/abc",
        "https://user:pw@hooks.slack.com/services/T0001/B0002/abc",
        "https://hooks.slack.com/services/T0001/B0002/abc?x=1",
        "https://hooks.slack.com/services/T0001/B0002",  # incomplete
        "https://hooks.slack.com/api/chat.postMessage",
        "https://hooks.slack.com/services/T0001/B0002/abc/../../x",
        URL + "a" * 500,
    ],
)
def test_other_urls_are_refused_without_repeating_them(url):
    with pytest.raises(ValueError) as info:
        check_slack_url(url)
    assert "abc" not in str(info.value) and "evil" not in str(info.value)


# --- what the messages look like ----------------------------------------------------------

PAYLOAD = {
    "event": "opened",
    "incident_id": "inc-1",
    "monitor_name": "Customer API",
    "target": "https://api.acme.com/health?token=SECRET",
    "cause": "HTTP 503 from <!channel> `boom`",
    "started_at": "2026-11-27T09:00:00Z",
    "resolved_at": None,
}


def all_text(message: dict) -> str:
    return json.dumps(message)


def test_opened_message():
    message = incident_message(PAYLOAD)
    assert message["text"] == "🔴 Customer API is down"
    text = all_text(message)
    assert "https://api.acme.com/health" in text
    assert "SECRET" not in text  # the query string is never shown
    assert "2026-11-27 09:00 UTC" in text
    assert "inc-1" in text


def interpreted_text(message: dict) -> str:
    """The parts Slack parses for mentions and links: everything except
    plain_text objects (the header), which Slack always shows as written."""
    blocks = [b for b in message["blocks"] if b["type"] != "header"]
    return json.dumps({"text": message["text"], "blocks": blocks})


def test_customer_text_cannot_ping_or_link():
    message = incident_message({**PAYLOAD, "monitor_name": "API <!here> & <https://evil.com|click>"})
    assert message["blocks"][0]["text"]["type"] == "plain_text"  # shown literally
    text = interpreted_text(message)
    assert "<!channel>" not in text and "<!here>" not in text
    assert "&lt;!channel&gt;" in text
    assert "<https://evil.com" not in text
    assert "`boom`" not in text  # backticks can't break out of the code block


def test_resolved_message_says_how_long_it_was_down():
    message = incident_message({**PAYLOAD, "event": "resolved", "resolved_at": "2026-11-27T09:12:30Z"})
    assert message["text"] == "🟢 Customer API is back up"
    text = all_text(message)
    assert "12m 30s" in text
    assert "Cause" not in text


def test_long_names_fit_slacks_limits():
    message = incident_message({**PAYLOAD, "monitor_name": "x" * 500, "cause": "y" * 5000})
    assert len(message["blocks"][0]["text"]["text"]) <= 150
    assert len(all_text(message)) < 3000


def test_escape():
    assert escape("a & <b>") == "a &amp; &lt;b&gt;"


def test_test_message_names_the_channel():
    message = channel_test_message("On-call <team>", datetime(2026, 11, 27, 9, 0, tzinfo=UTC))
    assert "On-call &lt;team&gt;" in all_text(message)


# --- sending -------------------------------------------------------------------------


def sender_answering(status: int, body: str = "ok", headers: dict | None = None, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        return httpx.Response(status, text=body, headers=headers or {})

    return HttpSlackSender(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_a_message_is_posted_as_json():
    calls: list[httpx.Request] = []
    sender = sender_answering(200, calls=calls)
    assert await sender.send(URL, {"text": "hi"}) == 200
    assert str(calls[0].url) == URL
    assert json.loads(calls[0].content) == {"text": "hi"}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (404, "no_service", "Slack rejected the message: 404 (no_service)"),
        (410, "channel_is_archived", "Slack rejected the message: 410 (channel_is_archived)"),
        (403, "<html>Forbidden " + URL + "</html>", "Slack rejected the message: 403"),
        (302, "", "Slack rejected the message: 302"),
    ],
)
async def test_rejections_are_permanent_and_safe_to_show(status, body, expected):
    with pytest.raises(PermanentSlackError) as info:
        await sender_answering(status, body).send(URL, {"text": "hi"})
    assert str(info.value) == expected
    assert classify_failure(info.value).kind is FailureKind.PERMANENT


@pytest.mark.parametrize("status", [429, 500, 503])
async def test_outages_and_rate_limits_are_retried(status):
    with pytest.raises(RetryableSlackError) as info:
        await sender_answering(status, headers={"Retry-After": "30"}).send(URL, {"text": "hi"})
    failure = classify_failure(info.value)
    assert (failure.kind, failure.retry_after) == (FailureKind.RETRYABLE, 30.0)


async def test_a_timeout_is_retried():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    sender = HttpSlackSender(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(RetryableSlackError):
        await sender.send(URL, {"text": "hi"})


async def test_a_bad_stored_url_is_never_contacted():
    calls: list[httpx.Request] = []
    with pytest.raises(PermanentSlackError):
        await sender_answering(200, calls=calls).send("https://evil.com/services/a/b/c", {"text": "hi"})
    assert calls == []
