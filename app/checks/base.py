from abc import ABC, abstractmethod

from app.checks.result import CheckResult


class BaseCheck(ABC):
    @abstractmethod
    async def run(self) -> CheckResult:
        pass
