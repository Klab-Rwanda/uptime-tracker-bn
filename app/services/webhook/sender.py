import json
import logging
import time
from collections.abc import Callable
from datetime import UTC
from typing import Any

import httpx

from app.services.webhook.base import (
    PermanentWebhookError,
    RetryableWebhookError,
    WebhookEvent,
    WebhookResult,
)
from app.services.webhook.signing import sign
from app.services.webhook.url_safety import (
    Resolver,
    UnsafeWebhookURL,
    resolve_safe_target,
)

logger = logging.getLogger(__name__)

USER_AGENT = "UpTimeTracker-Webhook/1.0"
RETRYABLE_STATUSES = {408, 425, 429}


def build_body(event: WebhookEvent) -> bytes:
    created = event.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    payload = {
        "id": event.delivery_id,
        "type": event.event_type,
        "created_at": created.astimezone(UTC).isoformat().replace("+00:00", "Z"),
        "data": event.data,
    }
    # Sorted and compact, so the exact bytes we sign are the bytes we send
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds >= 0 else None


class HttpWebhookSender:
    name = "webhook"

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
        timeout: float = 10.0,
        allow_private: bool = False,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._owns_client = client is None
        # trust_env=False: ignore proxy environment variables, which would
        # bypass the address checks. No redirects, ever.
        self._client = client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        self._resolver = resolver
        self._timeout = timeout
        self._allow_private = allow_private
        self._clock = clock

    async def send(self, url: str, secret: str, event: WebhookEvent) -> WebhookResult:
        if not secret:
            raise ValueError("A signing secret is required")

        try:
            target = await resolve_safe_target(
                url, resolver=self._resolver, allow_private=self._allow_private
            )
        except UnsafeWebhookURL as exc:
            raise PermanentWebhookError(f"Unsafe webhook URL: {exc}") from exc
        except OSError as exc:  # DNS failure or DNS timeout
            raise RetryableWebhookError("Could not resolve the webhook host") from exc

        body = build_body(event)
        timestamp = int(self._clock())
        headers = {
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
            "Host": target.host_header,
            "X-UpTime-Event": event.event_type,
            "X-UpTime-Delivery": event.delivery_id,
            "X-UpTime-Timestamp": str(timestamp),
            "X-UpTime-Signature": sign(secret, timestamp, body),
        }
        # We connect to the checked IP, so TLS must still be verified against
        # the real host name.
        extensions: dict[str, Any] | None = None
        if target.scheme == "https" and not target.host_is_ip:
            extensions = {"sni_hostname": target.host}

        started = time.perf_counter()
        urls = (target.request_url, *target.fallback_urls)
        for attempt, request_url in enumerate(urls, start=1):
            try:
                async with self._client.stream(
                    "POST",
                    request_url,
                    content=body,
                    headers=headers,
                    timeout=self._timeout,
                    follow_redirects=False,
                    extensions=extensions,
                ) as resp:
                    status = resp.status_code
                    retry_after = _parse_retry_after(resp.headers.get("retry-after"))
                break
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                # Nothing was sent yet, so trying the next checked IP can't
                # deliver twice
                if attempt < len(urls):
                    continue
                raise RetryableWebhookError(
                    "Could not connect to the webhook endpoint"
                ) from exc
            except httpx.TimeoutException as exc:
                raise RetryableWebhookError("Webhook request timed out") from exc
            except httpx.TransportError as exc:
                raise RetryableWebhookError(
                    "Could not connect to the webhook endpoint"
                ) from exc

        duration_ms = round((time.perf_counter() - started) * 1000, 1)

        if 200 <= status < 300:
            # Log the host only: webhook URLs often contain a secret token in the path
            logger.info(
                "webhook delivered",
                extra={
                    "host": target.host,
                    "status": status,
                    "delivery_id": event.delivery_id,
                    "duration_ms": duration_ms,
                },
            )
            return WebhookResult(status, duration_ms)

        if 300 <= status < 400:
            raise PermanentWebhookError("Endpoint returned a redirect (redirects are not followed)")
        if status in RETRYABLE_STATUSES or status >= 500:
            raise RetryableWebhookError(f"Endpoint returned {status}", retry_after=retry_after)
        raise PermanentWebhookError(f"Endpoint rejected the webhook ({status})")

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
