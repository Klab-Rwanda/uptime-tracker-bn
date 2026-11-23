from datetime import UTC, datetime

import pytest

from app.services.webhook.base import RetryableWebhookError, WebhookEvent
from app.services.webhook.fake import InMemoryWebhookSender

EVENT = WebhookEvent("incident.opened", "d1", datetime(2026, 11, 20, tzinfo=UTC), {})


async def test_records_deliveries():
    sender = InMemoryWebhookSender()
    result = await sender.send("https://hooks.acme.com/x", "secret", EVENT)
    assert sender.sent == [("https://hooks.acme.com/x", EVENT)]
    assert result.status_code == 200


async def test_can_simulate_failure():
    sender = InMemoryWebhookSender(fail_with=RetryableWebhookError("down"))
    with pytest.raises(RetryableWebhookError):
        await sender.send("https://hooks.acme.com/x", "secret", EVENT)
    assert sender.sent == []
