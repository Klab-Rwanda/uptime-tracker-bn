import pytest

from app.services.email.base import EmailMessage, RetryableEmailError
from app.services.email.fake import InMemoryEmailProvider

MESSAGE = EmailMessage(
    to=["a@b.com"], subject="s", html="<p>h</p>", text="t", idempotency_key="k1"
)


async def test_records_sent_messages():
    provider = InMemoryEmailProvider()
    result = await provider.send(MESSAGE)
    assert provider.sent == [MESSAGE]
    assert result.message_id == "memory-1"


async def test_can_simulate_failure():
    provider = InMemoryEmailProvider(fail_with=RetryableEmailError("down"))
    with pytest.raises(RetryableEmailError):
        await provider.send(MESSAGE)
    assert provider.sent == []
