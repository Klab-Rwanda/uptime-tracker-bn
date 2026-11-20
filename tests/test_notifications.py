from collections.abc import Iterator
from email.message import EmailMessage
from typing import ClassVar, Self

import pytest

from app.notifications.brevo import EmailDeliveryError, EmailKind, send_email
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
