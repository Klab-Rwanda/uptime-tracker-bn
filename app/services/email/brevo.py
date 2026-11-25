import logging

import httpx

from app.services.email.base import (
    EmailMessage,
    PermanentEmailError,
    RetryableEmailError,
    SendResult,
)

logger = logging.getLogger(__name__)

MAX_RECIPIENTS = 50


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class BrevoEmailProvider:
    name = "brevo"
    URL = "https://api.brevo.com/v3/smtp/email"

    def __init__(
        self,
        api_key: str,
        sender_email: str,
        sender_name: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ) -> None:
        if not api_key:
            raise ValueError("BREVO_API_KEY is not configured")
        if not sender_email:
            raise ValueError("BREVO_SENDER_EMAIL is not configured")
        self._api_key = api_key
        self._sender = {"email": sender_email, "name": sender_name}
        self._timeout = timeout
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient()

    async def send(self, message: EmailMessage) -> SendResult:
        if not message.to:
            raise PermanentEmailError("No recipients")
        if len(message.to) > MAX_RECIPIENTS:
            raise PermanentEmailError(f"Too many recipients (max {MAX_RECIPIENTS})")

        payload = {
            "sender": self._sender,
            "to": [{"email": address} for address in message.to],
            "subject": message.subject,
            "htmlContent": message.html,
            "textContent": message.text,
        }

        try:
            resp = await self._client.post(
                self.URL,
                json=payload,
                headers={"api-key": self._api_key, "accept": "application/json"},
                timeout=self._timeout,
            )
        except httpx.TimeoutException as exc:
            raise RetryableEmailError("Brevo request timed out") from exc
        except httpx.TransportError as exc:
            raise RetryableEmailError("Could not reach Brevo") from exc

        status = resp.status_code

        if 200 <= status < 300:
            message_id = None
            try:
                data = resp.json()
                if isinstance(data, dict):
                    message_id = data.get("messageId")
            except ValueError:
                pass
            # Log the number of recipients, never the addresses themselves
            logger.info(
                "email sent",
                extra={
                    "provider": self.name,
                    "message_id": message_id,
                    "recipient_count": len(message.to),
                    "idempotency_key": message.idempotency_key,
                },
            )
            return SendResult(self.name, message_id)

        detail = resp.text[:200]

        if status == 429 or status >= 500:
            raise RetryableEmailError(
                f"Brevo returned {status}",
                retry_after=_parse_retry_after(resp.headers.get("retry-after")),
            )

        if status in (401, 403):
            logger.error(
                "Brevo rejected the request: check the API key, authorized IPs "
                "and verified sender",
                extra={"status": status, "detail": detail},
            )
        raise PermanentEmailError(f"Brevo rejected the email ({status}): {detail}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
