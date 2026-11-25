import httpx
import pytest

from app.checks import http_check
from app.checks.http_check import MAX_BODY_BYTES, MAX_REDIRECTS, HTTPCheck

PUBLIC = "93.184.216.34"
OTHER_PUBLIC = "93.184.216.35"


def resolver_for(mapping: dict[str, list[str]]):
    async def resolve(host, port):
        return mapping.get(host, [PUBLIC])

    return resolve


def make_check(handler, *, target="https://status.acme.com/health", resolver=None, **kwargs):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HTTPCheck(
        monitor_id="m1",
        target=target,
        client=client,
        resolver=resolver or resolver_for({}),
        **kwargs,
    )


class Recorder:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request):
        self.requests.append(request)
        response = self.responses.pop(0) if self.responses else httpx.Response(200)
        if isinstance(response, Exception):
            raise response
        return response


# --- results ------------------------------------------------------------------


async def test_up_when_status_is_expected():
    result = await make_check(Recorder(httpx.Response(200))).run()
    assert result.status == "up"
    assert result.error_message is None
    assert result.checked_at.tzinfo is not None


async def test_down_on_unexpected_status():
    result = await make_check(Recorder(httpx.Response(503))).run()
    assert result.status == "down"
    assert result.error_message == "Unexpected status code: 503"


async def test_custom_expected_codes():
    result = await make_check(Recorder(httpx.Response(204)), expected_status_codes=[200, 204]).run()
    assert result.status == "up"


async def test_keyword_found_and_missing():
    ok = await make_check(Recorder(httpx.Response(200, text="all systems ok")), keyword="ok").run()
    missing = await make_check(Recorder(httpx.Response(200, text="error")), keyword="healthy").run()
    assert ok.status == "up"
    assert missing.status == "down"
    assert missing.error_message == "Keyword not found: 'healthy'"


async def test_only_the_first_megabyte_is_searched():
    page = "x" * MAX_BODY_BYTES + "needle"
    result = await make_check(Recorder(httpx.Response(200, text=page)), keyword="needle").run()
    assert result.status == "down"


async def test_timeout_is_down():
    rec = Recorder(httpx.ReadTimeout("slow"))
    result = await make_check(rec, timeout_seconds=5).run()
    assert result.status == "down"
    assert result.error_message == "Request timed out after 5s"


async def test_connection_error_hides_details():
    rec = Recorder(httpx.ConnectError("connect to 10.1.2.3:443 refused"), httpx.ConnectError("x"))
    resolver = resolver_for({"status.acme.com": [PUBLIC]})
    result = await make_check(rec, resolver=resolver).run()
    assert result.status == "down"
    assert result.error_message == "Connection failed (ConnectError)"
    assert "10.1.2.3" not in result.error_message


# --- the request we actually send ---------------------------------------------------


async def test_connects_to_the_checked_ip_with_the_real_host_name():
    rec = Recorder(httpx.Response(200))
    await make_check(rec).run()
    request = rec.requests[0]
    assert request.url.host == PUBLIC
    assert request.headers["host"] == "status.acme.com"
    assert request.extensions["sni_hostname"] == "status.acme.com"
    assert request.headers["user-agent"].startswith("UpTimeTracker-Monitor")


async def test_plain_http_targets_are_allowed():
    rec = Recorder(httpx.Response(200))
    result = await make_check(rec, target="http://status.acme.com/health").run()
    assert result.status == "up"
    assert rec.requests[0].url.scheme == "http"


async def test_falls_back_to_the_next_checked_ip():
    rec = Recorder(httpx.ConnectError("no IPv6 route"), httpx.Response(200))
    resolver = resolver_for({"status.acme.com": ["2606:4700::1", OTHER_PUBLIC]})
    result = await make_check(rec, resolver=resolver).run()
    assert result.status == "up"
    assert [r.url.host for r in rec.requests] == ["2606:4700::1", OTHER_PUBLIC]


async def test_head_requests_do_not_read_the_body():
    rec = Recorder(httpx.Response(200, text="ok"))
    result = await make_check(rec, method="HEAD", keyword="ok").run()
    assert rec.requests[0].method == "HEAD"
    assert result.status == "down"  # nothing to search in a HEAD response


# --- SSRF protection ------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1:8000/admin",
        "http://localhost/",
        "https://10.0.0.5/",
        "http://[::1]/",
        "ftp://files.acme.com/",
        "https://user:pw@status.acme.com/",
    ],
)
async def test_internal_or_odd_targets_are_refused_before_any_request(target):
    rec = Recorder()
    resolver = resolver_for({"localhost": ["127.0.0.1"]})
    result = await make_check(rec, target=target, resolver=resolver).run()
    assert result.status == "down"
    assert result.error_message.startswith("Target is not allowed")
    assert rec.requests == []


async def test_host_resolving_to_a_private_address_is_refused():
    rec = Recorder()
    resolver = resolver_for({"internal.acme.com": ["10.0.0.7"]})
    result = await make_check(rec, target="https://internal.acme.com/", resolver=resolver).run()
    assert result.status == "down"
    assert "10.0.0.7" not in result.error_message
    assert rec.requests == []


async def test_redirect_to_an_internal_address_is_refused():
    rec = Recorder(httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"}))
    result = await make_check(rec).run()
    assert result.status == "down"
    assert result.error_message.startswith("Target is not allowed")
    assert len(rec.requests) == 1  # the metadata address was never requested


async def test_public_redirects_are_followed_and_rechecked():
    rec = Recorder(
        httpx.Response(301, headers={"location": "https://www.acme.com/health"}),
        httpx.Response(200),
    )
    resolver = resolver_for({"www.acme.com": [OTHER_PUBLIC]})
    result = await make_check(rec, resolver=resolver).run()
    assert result.status == "up"
    assert rec.requests[1].url.host == OTHER_PUBLIC
    assert rec.requests[1].headers["host"] == "www.acme.com"


async def test_relative_redirects_work():
    rec = Recorder(httpx.Response(302, headers={"location": "/v2/health"}), httpx.Response(200))
    result = await make_check(rec).run()
    assert result.status == "up"
    assert rec.requests[1].url.path == "/v2/health"


async def test_an_expected_redirect_is_not_followed():
    rec = Recorder(httpx.Response(301, headers={"location": "https://elsewhere.com/"}))
    result = await make_check(rec, expected_status_codes=[301]).run()
    assert result.status == "up"
    assert len(rec.requests) == 1


async def test_redirect_loops_stop():
    loop = [httpx.Response(302, headers={"location": "/again"}) for _ in range(MAX_REDIRECTS + 2)]
    rec = Recorder(*loop)
    result = await make_check(rec).run()
    assert result.status == "down"
    assert result.error_message == f"More than {MAX_REDIRECTS} redirects"
    assert len(rec.requests) == MAX_REDIRECTS + 1


async def test_private_targets_work_in_dev_mode():
    rec = Recorder(httpx.Response(200))
    resolver = resolver_for({"localhost": ["127.0.0.1"]})
    result = await make_check(
        rec, target="http://localhost:9000/health", resolver=resolver, allow_private=True
    ).run()
    assert result.status == "up"
    assert rec.requests[0].url.host == "127.0.0.1"


async def test_default_client_ignores_proxy_settings(monkeypatch):
    created = {}
    real_client = httpx.AsyncClient

    def spy(**kwargs):
        created.update(kwargs)
        return real_client(transport=httpx.MockTransport(lambda r: httpx.Response(200)), **kwargs)

    monkeypatch.setattr(http_check.httpx, "AsyncClient", spy)
    check = HTTPCheck(monitor_id="m1", target="https://status.acme.com/", resolver=resolver_for({}))
    assert (await check.run()).status == "up"
    assert created == {"follow_redirects": False, "trust_env": False}
