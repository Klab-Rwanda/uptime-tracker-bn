from httpx import AsyncClient

# 404 is also fine: it avoids revealing that the resource exists.
DENIED = (403, 404)


async def assert_tenant_isolated(
    client: AsyncClient,
    method: str,
    url: str,
    *,
    outsider_headers: dict,
    json: dict | None = None,
) -> None:
    """Check that a user from another organization is refused."""
    resp = await client.request(method, url, headers=outsider_headers, json=json)
    assert resp.status_code in DENIED, (
        f"Tenant isolation broken: {method} {url} returned {resp.status_code}"
    )