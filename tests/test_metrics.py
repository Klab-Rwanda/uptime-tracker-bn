async def test_metrics_endpoint_format(client):
    resp = await client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    assert "# HELP" in resp.text


async def test_requests_are_counted(client):
    await client.get("/health/live")
    body = (await client.get("/metrics")).text
    assert 'http_requests_total{method="GET",path="/health/live",status="200"}' in body


async def test_latency_histogram_is_recorded(client):
    await client.get("/health/live")
    body = (await client.get("/metrics")).text
    assert "http_request_duration_seconds_bucket" in body


async def test_unknown_paths_share_one_label(client):
    await client.get("/random-path-123")
    await client.get("/another-random-path-456")
    body = (await client.get("/metrics")).text
    assert 'path="unmatched"' in body
    assert "random-path-123" not in body
    assert "another-random-path-456" not in body


async def test_metrics_endpoint_does_not_count_itself(client):
    await client.get("/metrics")
    body = (await client.get("/metrics")).text
    assert 'path="/metrics"' not in body
