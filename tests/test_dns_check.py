import dns.exception
import dns.resolver
import pytest

from app.checks import dns_check
from app.checks.dns_check import DNSCheck


def fake_resolver(error: Exception | None):
    class Resolver:
        lifetime = 0.0

        async def resolve(self, name, rdtype):
            if error is not None:
                raise error
            return ["93.184.216.34"]

    return Resolver


@pytest.mark.parametrize(
    ("error", "status", "message"),
    [
        (None, "up", None),
        (dns.resolver.NXDOMAIN(), "down", "Domain 'acme.com' does not exist"),
        (dns.exception.Timeout(), "down", "DNS lookup timed out after 5s"),
        (dns.resolver.NoAnswer(), "down", "DNS lookup failed (NoAnswer)"),
        (dns.resolver.NoNameservers(), "down", "DNS lookup failed (NoNameservers)"),
    ],
)
async def test_dns_results(monkeypatch, error, status, message):
    monkeypatch.setattr(dns_check.dns.asyncresolver, "Resolver", fake_resolver(error))
    result = await DNSCheck(monitor_id="m", target="acme.com", timeout_seconds=5).run()
    assert (result.status, result.error_message) == (status, message)
    assert result.checked_at.tzinfo is not None


async def test_bugs_are_not_reported_as_an_outage(monkeypatch):
    monkeypatch.setattr(dns_check.dns.asyncresolver, "Resolver", fake_resolver(KeyError("bug")))
    with pytest.raises(KeyError):
        await DNSCheck(monitor_id="m", target="acme.com").run()


async def test_uses_the_async_resolver():
    # The blocking resolver froze the whole server during every lookup
    import inspect

    source = inspect.getsource(DNSCheck.run)
    assert "dns.asyncresolver" in source
    assert "await resolver.resolve" in source
