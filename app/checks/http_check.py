import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from app.checks.base import BaseCheck
from app.checks.result import CheckResult
from app.services.webhook.url_safety import (
    Resolver,
    UnsafeWebhookURL,
    resolve_safe_target,
)

USER_AGENT = "UpTimeTracker-Monitor/1.0"
MAX_REDIRECTS = 5
# Only read this much of a page when looking for a keyword
MAX_BODY_BYTES = 1_000_000
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


class _TooManyRedirects(Exception):
    pass


class HTTPCheck(BaseCheck):
    """Checks a customer's HTTP(S) endpoint.

    Customers choose the target, so every request is protected like a webhook:
    each hop (including redirects) must resolve only to public addresses, and
    we connect to the exact IP we checked. That stops a monitor from making our
    servers fetch internal addresses such as cloud metadata (SSRF).
    """

    def __init__(
        self,
        monitor_id: str,
        target: str,
        method: str = "GET",
        expected_status_codes: list[int] | None = None,
        keyword: str | None = None,
        timeout_seconds: int = 30,
        *,
        allow_private: bool = False,
        client: httpx.AsyncClient | None = None,
        resolver: Resolver | None = None,
    ):
        self.monitor_id = monitor_id
        self.target = target
        self.method = method.upper()
        self.expected_status_codes = expected_status_codes or [200]
        self.keyword = keyword
        self.timeout_seconds = timeout_seconds
        self._allow_private = allow_private
        self._client = client
        self._resolver = resolver

    def _result(
        self, status: str, checked_at: datetime, start: float, error: str | None = None
    ) -> CheckResult:
        return CheckResult(
            monitor_id=self.monitor_id,
            status=status,
            response_time_ms=int((time.monotonic() - start) * 1000),
            checked_at=checked_at,
            error_message=error,
        )

    async def run(self) -> CheckResult:
        checked_at = datetime.now(UTC)
        start = time.monotonic()
        try:
            # One budget for everything: DNS, redirects and reading the body
            status_code, body = await asyncio.wait_for(
                self._fetch(), timeout=self.timeout_seconds
            )
        except UnsafeWebhookURL as exc:
            return self._result("down", checked_at, start, f"Target is not allowed: {exc}")
        except (TimeoutError, httpx.TimeoutException):
            return self._result(
                "down", checked_at, start, f"Request timed out after {self.timeout_seconds}s"
            )
        except _TooManyRedirects:
            return self._result("down", checked_at, start, f"More than {MAX_REDIRECTS} redirects")
        except (httpx.HTTPError, OSError) as exc:
            # The class name only: messages can contain internal addresses
            return self._result("down", checked_at, start, f"Connection failed ({type(exc).__name__})")

        if status_code not in self.expected_status_codes:
            return self._result("down", checked_at, start, f"Unexpected status code: {status_code}")
        if self.keyword and self.keyword not in body:
            return self._result("down", checked_at, start, f"Keyword not found: {self.keyword!r}")
        return self._result("up", checked_at, start)

    async def _fetch(self) -> tuple[int, str]:
        client = self._client or httpx.AsyncClient(follow_redirects=False, trust_env=False)
        try:
            url = self.target
            for _ in range(MAX_REDIRECTS + 1):
                status_code, location, body = await self._request_once(client, url)
                follow = (
                    status_code in REDIRECT_STATUSES
                    and status_code not in self.expected_status_codes
                    and location
                )
                if not follow:
                    return status_code, body
                url = urljoin(url, location)  # the next hop is checked again
            raise _TooManyRedirects
        finally:
            if self._client is None:
                await client.aclose()

    async def _request_once(
        self, client: httpx.AsyncClient, url: str
    ) -> tuple[int, str | None, str]:
        target = await resolve_safe_target(
            url,
            resolver=self._resolver,
            allow_private=self._allow_private,
            allow_http=True,
        )
        headers = {"Host": target.host_header, "User-Agent": USER_AGENT}
        extensions: dict[str, Any] | None = None
        if target.scheme == "https" and not target.host_is_ip:
            extensions = {"sni_hostname": target.host}  # verify TLS for the real name

        urls = (target.request_url, *target.fallback_urls)
        for attempt, request_url in enumerate(urls, start=1):
            try:
                async with client.stream(
                    self.method,
                    request_url,
                    headers=headers,
                    extensions=extensions,
                    follow_redirects=False,
                    timeout=self.timeout_seconds,
                ) as response:
                    body = ""
                    if self.keyword and self.method != "HEAD":
                        body = await _read_capped(response)
                    return response.status_code, response.headers.get("location"), body
            except (httpx.ConnectError, httpx.ConnectTimeout):
                # Nothing was sent yet; try the host's next checked address
                if attempt == len(urls):
                    raise
        raise AssertionError("unreachable")


async def _read_capped(response: httpx.Response) -> str:
    chunks: list[bytes] = []
    size = 0
    async for chunk in response.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size >= MAX_BODY_BYTES:
            break
    data = b"".join(chunks)[:MAX_BODY_BYTES]
    return data.decode(response.encoding or "utf-8", errors="replace")
