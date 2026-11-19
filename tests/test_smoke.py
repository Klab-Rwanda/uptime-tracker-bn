async def test_openapi_schema_is_served(client):
    resp = await client.get("/openapi.json")
    assert resp.status_code == 200
    assert "paths" in resp.json()