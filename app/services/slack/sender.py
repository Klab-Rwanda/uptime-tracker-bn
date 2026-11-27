"""Posting alerts to Slack through an "incoming webhook" URL.

A Slack incoming webhook is a secret URL like
https://hooks.slack.com/services/T000/B000/XXXX: anyone who has it can post to
that channel, so it is stored encrypted and never logged or shown.

Only Slack's own host is accepted. That makes this channel type safe from SSRF
(it can't be pointed at our internal network) without the IP checks generic
webhooks need.
"""

import logging
import re
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from app.services.webhook.base import PermanentWebhookError, RetryableWebhookError

logger = logging.getLogger(__name__)

SLACK_HOST = "hooks.slack.com"
MAX_URL_LENGTH = 500
_PATH_RE = re.compile(r"/services/[A-Za-z0-9]+/[A-Za-z0-9]+/[A-Za-z0-9]+")
# Slack answers errors with a short code like "no_service"; only such codes are
# safe to repeat in a message shown to users
_ERROR_CODE_RE = re.compile(r"[a-z_]{1,40}")
RETRYABLE_STATUSES = {408, 425, 429}


class RetryableSlackError(RetryableWebhookError):
    """Slack is down, slow or rate limiting us. Safe to retry."""


class PermanentSlackError(PermanentWebhookError):
    """Retrying won't help: the webhook was removed, the channel archived..."""


def check_slack_url(url: str) -> str:
    """The cleaned URL, or ValueError. Error messages never repeat the URL."""
    url = url.strip()
    if len(url) > MAX_URL_LENGTH:
        raise ValueError("webhook_url is too long")
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.hostname != SLACK_HOST
        or parts.port is not None
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
        or not _PATH_RE.fullmatch(parts.path)
    ):
        raise ValueError(
            "webhook_url must be a Slack incoming webhook: "
            "https://hooks.slack.com/services/..."
        )
    return url


def _parse_retry_after(value: str | None) -> float | None:
    try:
        seconds = float(value) if value else None
    except ValueError:
        return None
    return seconds if seconds is not None and seconds >= 0 else None


class SlackSender(Protocol):
    async def send(self, url: str, message: dict[str, Any]) -> int: ...

    async def aclose(self) -> None: ...


class HttpSlackSender:
    def __init__(self, *, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self._owns_client = client is None
        # No proxies from the environment, no redirects: we only ever talk to Slack
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._timeout = timeout

    async def send(self, url: str, message: dict[str, Any]) -> int:
        """Post one message; returns the HTTP status (200) or raises."""
        try:
            url = check_slack_url(url)
        except ValueError as exc:
            raise PermanentSlackError("The Slack webhook URL is not valid") from exc
        try:
            resp = await self._client.post(
                url, json=message, timeout=self._timeout, follow_redirects=False
            )
        except httpx.TimeoutException as exc:
            raise RetryableSlackError("Slack did not answer in time") from exc
        except httpx.TransportError as exc:
            raise RetryableSlackError("Could not connect to Slack") from exc

        status = resp.status_code
        if 200 <= status < 300:
            logger.info("slack message delivered", extra={"status": status})
            return status
        if status in RETRYABLE_STATUSES or status >= 500:
            raise RetryableSlackError(
                f"Slack returned {status}",
                retry_after=_parse_retry_after(resp.headers.get("retry-after")),
            )
        code = resp.text.strip()
        reason = f" ({code})" if _ERROR_CODE_RE.fullmatch(code) else ""
        # 404 no_service: the webhook was removed; 410 channel_is_archived...
        raise PermanentSlackError(f"Slack rejected the message: {status}{reason}")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
