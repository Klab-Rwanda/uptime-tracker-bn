from collections.abc import Iterator
from email.message import EmailMessage
from typing import ClassVar, Self

import pytest

from app.notifications.brevo import EmailDeliveryError, EmailKind, send_email
from app.services.email.base import EmailMessage as ApiEmailMessage
from app.services.email.base import PermanentEmailError, SendResult
from app.util.config import get_settings

pytestmark = pytest.mark.asyncio


class FakeSMTP:
    instances: ClassVar[list["FakeSMTP"]] = []

    def __init__(self, host: str, port: int, *, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.credentials: tuple[str, str] | None = None
        self.message: EmailMessage | None = None
        self.instances.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def ehlo(self) -> None:
        return None

    def starttls(self, *, context: object) -> None:
        self.started_tls = True

    def login(self, user: str, password: str) -> None:
        self.credentials = (user, password)

    def send_message(self, message: EmailMessage) -> None:
        self.message = message


@pytest.fixture
def configured_brevo(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("JWT_SECRET", "test-secret-value-that-is-at-least-32-bytes")
    monkeypatch.setenv("BREVO_SMTP_HOST", "smtp-relay.brevo.com")
    monkeypatch.setenv("BREVO_SMTP_PORT", "587")
    monkeypatch.setenv("BREVO_SMTP_LOGIN", "smtp-login@example.com")
    monkeypatch.setenv("BREVO_SMTP_KEY", "test-smtp-key")
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "noreply@example.com")
    monkeypatch.setenv("BREVO_SENDER_NAME", "UpTimeTracker")
    # No API key: these tests use SMTP even if a developer's .env has one
    monkeypatch.setenv("BREVO_API_KEY", "")
    get_settings.cache_clear()
    FakeSMTP.instances.clear()
    monkeypatch.setattr("app.notifications.brevo.smtplib.SMTP", FakeSMTP)
    yield
    get_settings.cache_clear()


async def test_sends_designed_otp_email_over_brevo_smtp(
    configured_brevo: None,
) -> None:
    await send_email(
        "otp_code",
        recipient="alice@example.com",
        parameters={
            "to_name": "Alice",
            "otp_code": "123456",
            "action": "verify your email address",
            "expires_in_minutes": "10",
        },
    )

    smtp = FakeSMTP.instances[0]
    assert smtp.host == "smtp-relay.brevo.com"
    assert smtp.port == 587
    assert smtp.started_tls
    assert smtp.credentials == ("smtp-login@example.com", "test-smtp-key")
    assert smtp.message is not None
    assert smtp.message["Subject"] == "Your UpTimeTracker verification code"
    assert smtp.message["To"] == "Alice <alice@example.com>"
    plain_body = smtp.message.get_body(preferencelist=("plain",))
    html_body = smtp.message.get_body(preferencelist=("html",))
    assert plain_body is not None
    assert html_body is not None
    assert "123456" in plain_body.get_content()
    assert "123456" in html_body.get_content()


@pytest.mark.parametrize(
    ("kind", "parameters", "expected_subject", "expected_content"),
    [
        (
            "notification",
            {"title": "Monitor is down", "message": "Production is unreachable."},
            "UpTimeTracker notification: Monitor is down",
            "Production is unreachable.",
        ),
        (
            "update",
            {"title": "Maintenance complete", "message": "All systems are healthy."},
            "UpTimeTracker update: Maintenance complete",
            "All systems are healthy.",
        ),
        (
            "invite",
            {
                "inviter_name": "Alex",
                "organization_name": "Example Team",
                "role": "admin",
                "invite_url": "https://example.com/invite/abc",
            },
            "Invitation to join Example Team on UpTimeTracker",
            "https://example.com/invite/abc",
        ),
    ],
)
async def test_builds_notification_update_and_invite_messages(
    configured_brevo: None,
    kind: EmailKind,
    parameters: dict[str, str],
    expected_subject: str,
    expected_content: str,
) -> None:
    await send_email(kind, recipient="alice@example.com", parameters=parameters)

    message = FakeSMTP.instances[0].message
    assert message is not None
    assert message["Subject"] == expected_subject
    plain_body = message.get_body(preferencelist=("plain",))
    assert plain_body is not None
    assert expected_content in plain_body.get_content()


async def test_rejects_invalid_invite_url_before_sending(
    configured_brevo: None,
) -> None:
    with pytest.raises(EmailDeliveryError, match="absolute HTTP or HTTPS URL"):
        await send_email(
            "invite",
            recipient="alice@example.com",
            parameters={
                "inviter_name": "Alex",
                "organization_name": "Example Team",
                "invite_url": "javascript:alert(1)",
            },
        )

    assert not FakeSMTP.instances


# --- sending through the Brevo HTTP API ---------------------------------------------


class FakeApiProvider:
    sent: ClassVar[list[ApiEmailMessage]] = []
    closed: ClassVar[int] = 0
    fail_with: ClassVar[Exception | None] = None

    def __init__(self, api_key: str, sender_email: str, sender_name: str) -> None:
        self.settings = (api_key, sender_email, sender_name)

    async def send(self, message: ApiEmailMessage) -> SendResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        return SendResult(provider="brevo", message_id="msg-1")

    async def aclose(self) -> None:
        FakeApiProvider.closed += 1


@pytest.fixture
def api_brevo(configured_brevo: None, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("BREVO_API_KEY", "test-api-key")
    monkeypatch.setenv("BREVO_SMTP_LOGIN", "")  # SMTP is not set up at all
    monkeypatch.setenv("BREVO_SMTP_KEY", "")
    get_settings.cache_clear()
    FakeApiProvider.sent.clear()
    FakeApiProvider.closed = 0
    FakeApiProvider.fail_with = None
    monkeypatch.setattr("app.notifications.brevo.BrevoEmailProvider", FakeApiProvider)
    yield


async def test_with_an_api_key_the_otp_email_goes_through_the_api(api_brevo: None) -> None:
    await send_email(
        "otp_code",
        recipient="ada@example.com",
        parameters={"otp_code": "123456", "action": "verify your email", "expires_in_minutes": "10", "to_name": "Ada"},
    )
    [message] = FakeApiProvider.sent
    assert message.to == ["ada@example.com"]
    assert message.subject == "Your UpTimeTracker verification code"
    assert "123456" in message.html and "123456" in message.text
    assert message.idempotency_key.startswith("otp_code:")
    assert FakeSMTP.instances == []  # SMTP was not used
    assert FakeApiProvider.closed == 1


async def test_api_failures_become_delivery_errors(api_brevo: None) -> None:
    FakeApiProvider.fail_with = PermanentEmailError("Brevo rejected the request (401)")
    with pytest.raises(EmailDeliveryError):
        await send_email(
            "notification", recipient="ada@example.com", parameters={"title": "Hi", "message": "Body"}
        )
    assert FakeApiProvider.closed == 1


async def test_api_needs_a_sender(api_brevo: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BREVO_SENDER_EMAIL", "")
    get_settings.cache_clear()
    with pytest.raises(EmailDeliveryError):
        await send_email(
            "notification", recipient="ada@example.com", parameters={"title": "Hi", "message": "Body"}
        )
    assert FakeApiProvider.sent == []
