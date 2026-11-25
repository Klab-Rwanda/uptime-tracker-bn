import pytest

from app.services.webhook.url_safety import (
    UnsafeWebhookURL,
    check_url_syntax,
    is_public_ip,
    resolve_safe_target,
)

PUBLIC = "93.184.216.34"


async def public_resolver(host, port):
    return [PUBLIC]


def fixed_resolver(*ips):
    async def resolver(host, port):
        return list(ips)

    return resolver


async def must_not_be_called(host, port):
    raise AssertionError("resolver should not have been called")


@pytest.mark.parametrize(
    "ip", ["8.8.8.8", "93.184.216.34", "2606:4700:4700::1111", "::ffff:8.8.8.8"]
)
def test_public_addresses_are_allowed(ip):
    assert is_public_ip(ip)


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",  # cloud metadata
        "0.0.0.0",
        "100.64.0.1",  # carrier-grade NAT
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "64:ff9b::7f00:1",  # NAT64 wrapping 127.0.0.1
        "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
        "not-an-ip",
    ],
)
def test_non_public_addresses_are_blocked(ip):
    assert not is_public_ip(ip)


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/hook",
        "ftp://example.com/hook",
        "example.com/hook",
        "javascript:alert(1)",
        "https://user:pw@example.com/hook",
        "https://user@example.com/hook",
        "https://",
        "https:///hook",
        "",
        "https://exa mple.com/",
        "https://example.com/\nX-Evil: 1",
        "https://example.com:99999/",
        "https://example.com:abc/",
        "https://" + "a" * 3000 + ".com/",
    ],
)
def test_bad_urls_are_rejected(url):
    with pytest.raises(UnsafeWebhookURL):
        check_url_syntax(url)


def test_good_url_is_parsed_and_normalised():
    parsed = check_url_syntax("https://Hooks.Acme.com/path?x=1")
    assert parsed.host == "hooks.acme.com"
    assert parsed.port == 443
    assert parsed.path == "/path"
    assert parsed.query == "x=1"


def test_private_mode_allows_http():
    parsed = check_url_syntax("http://localhost:9000/hook", allow_private=True)
    assert parsed.scheme == "http"
    assert parsed.port == 9000


async def test_public_host_is_pinned_to_the_checked_ip():
    target = await resolve_safe_target("https://hooks.acme.com/a?b=1", resolver=public_resolver)
    assert target.ip == PUBLIC
    assert target.request_url == f"https://{PUBLIC}/a?b=1"
    assert target.host_header == "hooks.acme.com"
    assert target.host == "hooks.acme.com"
    assert target.host_is_ip is False


async def test_non_default_port_is_kept():
    target = await resolve_safe_target(
        "https://hooks.acme.com:8443/x?y=1", resolver=public_resolver
    )
    assert target.request_url == f"https://{PUBLIC}:8443/x?y=1"
    assert target.host_header == "hooks.acme.com:8443"


async def test_empty_path_becomes_slash():
    target = await resolve_safe_target("https://hooks.acme.com", resolver=public_resolver)
    assert target.request_url == f"https://{PUBLIC}/"


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "192.168.1.10", "::1", "::ffff:127.0.0.1"]
)
async def test_hosts_resolving_to_private_addresses_are_blocked(ip):
    with pytest.raises(UnsafeWebhookURL, match="non-public") as info:
        await resolve_safe_target("https://evil.example.com/", resolver=fixed_resolver(ip))
    assert ip not in str(info.value)  # never leak resolved internal IPs


async def test_one_private_record_among_public_ones_blocks_the_host():
    with pytest.raises(UnsafeWebhookURL):
        await resolve_safe_target(
            "https://mixed.example.com/", resolver=fixed_resolver(PUBLIC, "10.0.0.5")
        )


async def test_numeric_host_tricks_are_caught_after_resolution():
    # "2130706433" is another way to write 127.0.0.1; the OS resolver expands it
    with pytest.raises(UnsafeWebhookURL):
        await resolve_safe_target("https://2130706433/", resolver=fixed_resolver("127.0.0.1"))


@pytest.mark.parametrize("url", ["https://127.0.0.1/hook", "https://[::1]/hook", "https://10.1.2.3/x"])
async def test_private_ip_literals_are_blocked_without_dns(url):
    with pytest.raises(UnsafeWebhookURL):
        await resolve_safe_target(url, resolver=must_not_be_called)


async def test_public_ip_literal_is_allowed():
    target = await resolve_safe_target(f"https://{PUBLIC}/hook", resolver=must_not_be_called)
    assert target.host_is_ip is True
    assert target.request_url == f"https://{PUBLIC}/hook"


async def test_other_checked_ips_are_kept_as_fallbacks():
    target = await resolve_safe_target(
        "https://hooks.acme.com/a", resolver=fixed_resolver("2606:4700::1", PUBLIC)
    )
    assert target.request_url == "https://[2606:4700::1]/a"
    assert target.fallback_urls == (f"https://{PUBLIC}/a",)


async def test_unresolvable_host_is_rejected():
    with pytest.raises(UnsafeWebhookURL):
        await resolve_safe_target("https://nothing.example.com/", resolver=fixed_resolver())


async def test_private_mode_allows_local_targets():
    target = await resolve_safe_target(
        "http://localhost:9000/hook",
        resolver=fixed_resolver("127.0.0.1"),
        allow_private=True,
    )
    assert target.request_url == "http://127.0.0.1:9000/hook"
    assert target.host_header == "localhost:9000"
