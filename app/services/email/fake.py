from app.services.email.base import EmailMessage, SendResult


class InMemoryEmailProvider:
    """Records emails instead of sending them. Use in tests and end-to-end runs."""

    name = "memory"

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[EmailMessage] = []
        self.fail_with = fail_with

    async def send(self, message: EmailMessage) -> SendResult:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(message)
        return SendResult(self.name, f"memory-{len(self.sent)}")

    async def aclose(self) -> None:
        return None
