from typing import Any


class InMemorySlackSender:
    """Records Slack messages instead of sending them. For tests and end-to-end runs."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []
        self.fail_with = fail_with

    async def send(self, url: str, message: dict[str, Any]) -> int:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append((url, message))
        return 200

    async def aclose(self) -> None:
        return None
