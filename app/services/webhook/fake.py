from app.services.webhook.base import WebhookEvent, WebhookResult


class InMemoryWebhookSender:
    """Records deliveries instead of sending them. For tests and end-to-end runs."""

    name = "memory"

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, WebhookEvent]] = []
        self.fail_with = fail_with

    async def send(self, url: str, secret: str, event: WebhookEvent) -> WebhookResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((url, event))
        return WebhookResult(status_code=200, duration_ms=0.0)

    async def aclose(self) -> None:
        return None
