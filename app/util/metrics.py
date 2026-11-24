import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from starlette.middleware.base import BaseHTTPMiddleware

REQUESTS = Counter(
    "http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds",
    ["method", "path"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Don't count the metrics endpoint scraping itself
        if request.url.path == "/metrics":
            return await call_next(request)

        start = time.perf_counter()
        status = 500  # used if the request crashes before a response exists
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            # Use the route TEMPLATE ("/v1/monitors/{monitor_id}"), not the real URL,
            # otherwise every ID creates a new label value.
            route = request.scope.get("route")
            path = getattr(route, "path", "unmatched")
            elapsed = time.perf_counter() - start
            REQUESTS.labels(request.method, path, str(status)).inc()
            LATENCY.labels(request.method, path).observe(elapsed)


async def metrics_endpoint() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


def register_metrics(app: FastAPI) -> None:
    app.add_middleware(MetricsMiddleware)
    app.add_api_route(
        "/metrics", metrics_endpoint, methods=["GET"], include_in_schema=False
    )
