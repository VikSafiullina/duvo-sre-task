"""Structured JSON logs correlated with OpenTelemetry traces, plus RED request metrics.

Trace and log *export* is configured by `opentelemetry-instrument` (see Dockerfile/compose).
This module only shapes stdout so `docker logs` / Cloud Logging stay greppable.
"""

import json
import logging
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from fastapi import Request, Response
from opentelemetry import trace

from app.metrics import HTTP_LATENCY, HTTP_REQUESTS

_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "severity": record.levelname,  # `severity` is what Cloud Logging parses
            "logger": record.name,
            "msg": record.getMessage(),
        }
        span = trace.get_current_span().get_span_context()
        if span.is_valid:
            payload["trace_id"] = format(span.trace_id, "032x")
            payload["span_id"] = format(span.span_id, "016x")
        extras = {k: v for k, v in vars(record).items() if k not in _RESERVED}
        payload.update({k: v for k, v in extras.items() if not k.startswith("otel")})
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(level: str) -> None:
    """Send `app.*` logs to stdout as JSON. Propagation stays on, so the OTel handler that
    auto-instrumentation installs on the root logger also ships them to Loki."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("app")
    logger.handlers[:] = [handler]
    logger.setLevel(level)


_http_log = logging.getLogger("app.http")
_QUIET_ROUTES = {"/healthz", "/readyz", "/metrics"}
# Any token is a valid HTTP method, so the method is caller input: unknown ones collapse to
# OTHER, or `curl -X <random>` would mint a new time series per request.
_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})


async def observe_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """RED metrics + one structured access-log line per request, keyed by route template."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers["x-request-id"] = request_id
        return response
    finally:
        route = getattr(request.scope.get("route"), "path", "unmatched")
        elapsed = time.perf_counter() - start
        method = request.method if request.method in _METHODS else "OTHER"
        HTTP_REQUESTS.labels(method, route, str(status)).inc()
        HTTP_LATENCY.labels(method, route).observe(elapsed)
        if route not in _QUIET_ROUTES:
            _http_log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "route": route,
                    "status": status,
                    "duration_ms": round(elapsed * 1000, 1),
                },
            )
