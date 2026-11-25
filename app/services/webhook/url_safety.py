import asyncio
import ipaddress
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

DEFAULT_PORTS = {"http": 80, "https": 443}
MAX_URL_LENGTH = 2048
DNS_TIMEOUT_SECONDS = 5.0

# Ranges that can hide an internal IPv4 address inside an IPv6 one
_EXTRA_BLOCKED = (
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64
    ipaddress.ip_network("2002::/16"),  # 6to4
)

Resolver = Callable[[str, int], Awaitable[list[str]]]


class UnsafeWebhookURL(ValueError):
    """The URL is not allowed as a webhook target.

    Messages never contain the URL (it may hold a secret token) or any IP
    (it could reveal internal DNS to the person who typed the URL).
    """


@dataclass(frozen=True)
class ParsedURL:
    scheme: str
    host: str  # lowercase ASCII, no brackets
    port: int
    path: str
    query: str


@dataclass(frozen=True)
class SafeTarget:
    scheme: str
    host: str  # the real host name (for the Host header and TLS)
    ip: str  # the checked IP we will actually connect to
    port: int
    request_url: str  # URL with the host replaced by the checked IP
    host_header: str
    host_is_ip: bool
    # Same URL for the other checked IPs, tried in order if connecting fails
    # (e.g. an IPv6 address listed first on a server with no IPv6 route)
    fallback_urls: tuple[str, ...] = ()


def is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if any(addr in net for net in _EXTRA_BLOCKED):
        return False
    return addr.is_global and not (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_multicast
        or addr.is_reserved
        or addr.is_unspecified
    )


def _literal_ip(host: str) -> str | None:
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None


def check_url_syntax(
    url: str, *, allow_private: bool = False, allow_http: bool = False
) -> ParsedURL:
    """Checks that need no DNS. Also usable when a channel is created or updated.

    Webhooks must use https. Health checks may monitor plain http sites, so they
    pass allow_http=True; the address checks stay the same either way.
    """
    if not url or len(url) > MAX_URL_LENGTH:
        raise UnsafeWebhookURL("URL is empty or too long")
    if any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise UnsafeWebhookURL("URL contains whitespace or control characters")

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise UnsafeWebhookURL("URL is malformed") from None

    allowed = {"http", "https"} if (allow_private or allow_http) else {"https"}
    if parts.scheme not in allowed:
        raise UnsafeWebhookURL(
            "URL must use http or https" if allow_http else "URL must use https"
        )
    if parts.username is not None or parts.password is not None:
        raise UnsafeWebhookURL("URL must not contain credentials")

    host = parts.hostname
    if not host:
        raise UnsafeWebhookURL("URL has no host")
    if _literal_ip(host) is None:
        try:
            host = host.encode("idna").decode("ascii")
        except UnicodeError:
            raise UnsafeWebhookURL("URL host is invalid") from None

    return ParsedURL(
        scheme=parts.scheme,
        host=host,
        port=port or DEFAULT_PORTS[parts.scheme],
        path=parts.path or "/",
        query=parts.query,
    )


async def system_resolver(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    infos = await asyncio.wait_for(
        loop.getaddrinfo(host, port, type=socket.SOCK_STREAM),
        timeout=DNS_TIMEOUT_SECONDS,
    )
    ips: list[str] = []
    for info in infos:
        ip = str(info[4][0]).split("%", 1)[0]  # drop IPv6 zone ids
        if ip not in ips:
            ips.append(ip)
    return ips


async def resolve_safe_target(
    url: str,
    *,
    resolver: Resolver | None = None,
    allow_private: bool = False,
    allow_http: bool = False,
) -> SafeTarget:
    """Validate the URL, resolve it, and return the IP that is safe to connect to.

    The request URL is rebuilt from the checked pieces, so what we send to can't
    differ from what we validated (no parser tricks, no DNS rebinding).
    """
    parsed = check_url_syntax(url, allow_private=allow_private, allow_http=allow_http)

    literal = _literal_ip(parsed.host)
    if literal is not None:
        ips = [literal]
    else:
        ips = await (resolver or system_resolver)(parsed.host, parsed.port)

    if not ips:
        raise UnsafeWebhookURL("URL host did not resolve to an address")
    if not allow_private and not all(is_public_ip(ip) for ip in ips):
        raise UnsafeWebhookURL("URL host resolves to a non-public address")

    host_netloc = f"[{parsed.host}]" if ":" in parsed.host else parsed.host
    suffix = "" if parsed.port == DEFAULT_PORTS[parsed.scheme] else f":{parsed.port}"

    def url_for(ip: str) -> str:
        ip_netloc = f"[{ip}]" if ":" in ip else ip
        return urlunsplit((parsed.scheme, ip_netloc + suffix, parsed.path, parsed.query, ""))

    return SafeTarget(
        scheme=parsed.scheme,
        host=parsed.host,
        ip=ips[0],
        port=parsed.port,
        request_url=url_for(ips[0]),
        host_header=host_netloc + suffix,
        host_is_ip=literal is not None,
        fallback_urls=tuple(url_for(ip) for ip in ips[1:]),
    )
