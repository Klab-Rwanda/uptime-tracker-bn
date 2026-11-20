import asyncio
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr
from html import escape
from typing import Literal
from urllib.parse import urlsplit

from app.util.config import get_settings

EmailKind = Literal["otp_code", "notification", "update", "invite"]
logger = logging.getLogger(__name__)


class EmailDeliveryError(Exception):
    pass


def _required(parameters: dict[str, str], key: str) -> str:
    value = parameters.get(key, "").strip()
    if not value:
        raise EmailDeliveryError(f"Email parameter {key} is required")
    return value


def _subject(value: str) -> str:
    cleaned = " ".join(value.split())
    if not cleaned or len(cleaned) > 200:
        raise EmailDeliveryError("Email subject must be between 1 and 200 characters")
    return cleaned


def _html_document(title: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="en">
  <body style="margin:0;background:#f3f6fa;font-family:Arial,sans-serif;color:#1f2937">
    <table role="presentation" width="100%" cellspacing="0" cellpadding="0">
      <tr><td align="center" style="padding:32px 16px">
        <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
          style="max-width:560px;background:#ffffff;border-radius:12px">
          <tr><td style="padding:32px">
            <p style="margin:0 0 24px;color:#2563eb;font-weight:bold">UpTimeTracker</p>
            <h1 style="font-size:22px;margin:0 0 20px">{escape(title)}</h1>
            {content}
            <p style="margin:28px 0 0;color:#6b7280;font-size:13px">
              This is an automated message from UpTimeTracker.
            </p>
          </td></tr>
        </table>
      </td></tr>
    </table>
  </body>
</html>"""


def _build_message(
    kind: EmailKind,
    *,
    recipient: str,
    parameters: dict[str, str],
    sender_email: str,
    sender_name: str,
) -> EmailMessage:
    recipient_name = parameters.get("to_name", "").strip()
    greeting = escape(recipient_name or "there")
    message = EmailMessage()
    message["From"] = formataddr((sender_name, sender_email))
    message["To"] = formataddr((recipient_name, recipient))

    if kind == "otp_code":
        code = _required(parameters, "otp_code")
        if len(code) != 6 or not code.isdigit():
            raise EmailDeliveryError("OTP email requires a six-digit code")
        action = _required(parameters, "action")
        try:
            expires_in = int(_required(parameters, "expires_in_minutes"))
        except ValueError as error:
            raise EmailDeliveryError("OTP expiry must be an integer") from error
        if expires_in < 1:
            raise EmailDeliveryError("OTP expiry must be positive")

        title = (
            "Your UpTimeTracker password reset code"
            if action == "reset your password"
            else "Your UpTimeTracker verification code"
        )
        message["Subject"] = title
        text = (
            f"Hello {recipient_name or 'there'},\n\n"
            f"Use this code to {action}: {code}\n"
            f"It expires in {expires_in} minutes. If you did not request this, "
            "you can ignore this email."
        )
        content = (
            f"<p>Hello {greeting},</p>"
            f"<p>Use this code to {escape(action)}:</p>"
            "<div style=\"margin:24px 0;padding:16px;background:#eff6ff;"
            "border-radius:8px;text-align:center;font-size:32px;"
            f"font-weight:bold;letter-spacing:8px;color:#1d4ed8\">{code}</div>"
            f"<p>This code expires in {expires_in} minutes. If you did not "
            "request it, you can ignore this email.</p>"
        )
    elif kind in ("notification", "update"):
        title = _required(parameters, "title")
        body = _required(parameters, "message")
        subject_prefix = (
            "UpTimeTracker notification"
            if kind == "notification"
            else "UpTimeTracker update"
        )
        message["Subject"] = _subject(f"{subject_prefix}: {title}")
        text = f"Hello {recipient_name or 'there'},\n\n{title}\n\n{body}"
        content = (
            f"<p>Hello {greeting},</p>"
            f"<h2 style=\"font-size:18px\">{escape(title)}</h2>"
            f"<p style=\"white-space:pre-line\">{escape(body)}</p>"
        )
    else:
        inviter = _required(parameters, "inviter_name")
        organization = _required(parameters, "organization_name")
        invite_url = _required(parameters, "invite_url")
        parsed_url = urlsplit(invite_url)
        if parsed_url.scheme not in ("http", "https") or not parsed_url.netloc:
            raise EmailDeliveryError("Invite URL must be an absolute HTTP or HTTPS URL")
        role = parameters.get("role", "").strip()
        title = f"Invitation to join {organization}"
        message["Subject"] = _subject(f"{title} on UpTimeTracker")
        role_text = f"\nRole: {role}" if role else ""
        text = (
            f"Hello {recipient_name or 'there'},\n\n"
            f"{inviter} invited you to join {organization} on UpTimeTracker."
            f"{role_text}\n\nAccept the invitation: {invite_url}"
        )
        role_html = f"<p>Role: {escape(role)}</p>" if role else ""
        content = (
            f"<p>Hello {greeting},</p>"
            f"<p><strong>{escape(inviter)}</strong> invited you to join "
            f"<strong>{escape(organization)}</strong> on UpTimeTracker.</p>"
            f"{role_html}"
            f"<p><a href=\"{escape(invite_url, quote=True)}\" "
            "style=\"display:inline-block;padding:12px 20px;background:#2563eb;"
            "color:#ffffff;text-decoration:none;border-radius:6px\">"
            "Accept invitation</a></p>"
        )

    message.set_content(text)
    message.add_alternative(_html_document(title, content), subtype="html")
    return message


def _send_smtp(message: EmailMessage) -> None:
    settings = get_settings()
    tls_context = ssl.create_default_context()
    smtp_client: smtplib.SMTP | smtplib.SMTP_SSL
    if settings.brevo_smtp_port == 465:
        smtp_client = smtplib.SMTP_SSL(
            settings.brevo_smtp_host,
            settings.brevo_smtp_port,
            timeout=10,
            context=tls_context,
        )
    else:
        smtp_client = smtplib.SMTP(
            settings.brevo_smtp_host,
            settings.brevo_smtp_port,
            timeout=10,
        )

    with smtp_client as server:
        if settings.brevo_smtp_port != 465:
            server.ehlo()
            server.starttls(context=tls_context)
            server.ehlo()
        server.login(
            settings.brevo_smtp_login,
            settings.brevo_smtp_key.get_secret_value(),
        )
        server.send_message(message)


async def send_email(
    kind: EmailKind,
    *,
    recipient: str,
    parameters: dict[str, str],
) -> None:
    settings = get_settings()
    if not all(
        (
            settings.brevo_smtp_host,
            settings.brevo_smtp_login,
            settings.brevo_smtp_key.get_secret_value(),
            settings.brevo_sender_email,
            settings.brevo_sender_name,
        )
    ):
        logger.error("Brevo SMTP is not configured for email kind %s", kind)
        raise EmailDeliveryError("Brevo SMTP is not fully configured")
    if settings.brevo_smtp_port not in (465, 587, 2525):
        raise EmailDeliveryError("Brevo SMTP port must be 465, 587, or 2525")

    message = _build_message(
        kind,
        recipient=recipient,
        parameters=parameters,
        sender_email=settings.brevo_sender_email,
        sender_name=settings.brevo_sender_name,
    )
    try:
        await asyncio.to_thread(_send_smtp, message)
    except smtplib.SMTPResponseException as error:
        logger.warning(
            "Brevo SMTP rejected email kind %s with status %s",
            kind,
            error.smtp_code,
        )
        raise EmailDeliveryError("Brevo SMTP rejected the email") from error
    except (smtplib.SMTPException, OSError) as error:
        logger.warning(
            "Brevo SMTP failed for email kind %s with %s",
            kind,
            type(error).__name__,
        )
        raise EmailDeliveryError("Brevo SMTP could not deliver the email") from error
