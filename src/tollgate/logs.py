"""Structured JSON logs, each line tagged with the request it belongs to.

Every request gets an id: the caller's `X-Request-ID` if it's well formed, otherwise a new
one. The id is echoed back in the response and added to every log line written while the
request is handled, so one search in CloudWatch shows everything that happened to it.

Each line also carries the trace and span it was written inside, so a slow request found
on a dashboard and the log lines that explain it can be put side by side without guessing.

This middleware opens the root span as well as the request id, because both have to cover
the whole request - including the part of a streamed response that is produced long after
the handler has returned.

Rule: never log prompt or response text, headers, or query strings (a tenant may put its
key in `?key=`). The same rule governs span attributes; see telemetry.py. Metadata only.
"""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from opentelemetry.propagate import extract
from opentelemetry.trace import SpanKind, Status, StatusCode
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from tollgate.telemetry import (
    RequestFacts,
    current_ids,
    emit_request_metrics,
    facts_var,
    header_carrier,
    tracer,
)

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_var: ContextVar[str | None] = ContextVar("tenant", default=None)

REQUEST_ID_HEADER = "x-request-id"
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# The service talking to itself and its operators, rather than a tenant using it.
# Health checks arrive every few seconds and a metrics scrape does too, so counting them
# buries real traffic in the logs - and, because they carry no tenant or model, adds an
# "anonymous"/"none" series to every dashboard panel that is purely the gateway watching
# itself. Excluded from the access log and from the metrics alike.
UNINSTRUMENTED_PATHS = frozenset({"/livez", "/readyz", "/metrics"})

access_logger = logging.getLogger("tollgate.access")


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Extra fields go in `extra={"fields": {...}}`."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if request_id := request_id_var.get():
            entry["request_id"] = request_id
        if tenant := tenant_var.get():
            entry["tenant"] = tenant
        if (ids := current_ids()) is not None:
            entry["trace_id"], entry["span_id"] = ids
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            entry.update(fields)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str)


def incoming_request_id(scope: Scope) -> str:
    for name, value in scope.get("headers", []):
        if name == REQUEST_ID_HEADER.encode():
            candidate: str = bytes(value).decode("latin-1")
            if VALID_REQUEST_ID.match(candidate):
                return candidate
            break
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Assigns the request id, opens the root span, and writes one access log line.

    A plain ASGI middleware rather than Starlette's BaseHTTPMiddleware, so it passes
    streaming responses through untouched (phase 3 depends on that). That matters twice
    over here: the root span stays open until the last event of a stream has been
    relayed, so the upstream time recorded beneath it covers the whole response rather
    than the moment before the first byte.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = incoming_request_id(scope)
        request_token = request_id_var.set(request_id)
        tenant_token = tenant_var.set(None)
        # Mutated in place by whatever the request turns out to do, and read once below.
        facts_token = facts_var.set(RequestFacts())
        started = time.perf_counter()
        status_code = 500  # if the app raises before responding, that's what the caller sees

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        # Continue the caller's trace when they sent one, so a client that is already
        # instrumented sees the gateway inside its own request rather than as a gap
        # followed by an unrelated trace.
        context = extract(header_carrier(scope.get("headers", [])))
        try:
            with tracer.start_as_current_span(
                f"{scope['method']} {scope['path']}",  # the path, never the query string
                context=context,
                kind=SpanKind.SERVER,
                attributes={
                    "http.request.method": scope["method"],
                    "url.path": scope["path"],
                    "tollgate.request_id": request_id,
                },
            ) as span:
                try:
                    await self.app(scope, receive, send_with_request_id)
                finally:
                    span.set_attribute("http.response.status_code", status_code)
                    if tenant := tenant_var.get():
                        span.set_attribute("tollgate.tenant", tenant)
                    if status_code >= 500:
                        # A 4xx is the caller being told something, not a gateway failure.
                        span.set_status(Status(StatusCode.ERROR, str(status_code)))
                    elapsed_ms = (time.perf_counter() - started) * 1000
                    # Here rather than in the proxy, so a request refused before it ever
                    # reached the proxy - a 401, a 429, a 402 - is still counted.
                    if scope["path"] not in UNINSTRUMENTED_PATHS:
                        emit_request_metrics(tenant_var.get(), status_code, elapsed_ms)
                    self.log_access(scope, request_id, started, status_code)
        finally:
            facts_var.reset(facts_token)
            tenant_var.reset(tenant_token)
            request_id_var.reset(request_token)

    def log_access(self, scope: Scope, request_id: str, started: float, status: int) -> None:
        if scope["path"] in UNINSTRUMENTED_PATHS:
            return
        access_logger.info(
            "request",
            extra={
                "fields": {
                    "request_id": request_id,
                    "tenant": tenant_var.get(),
                    "method": scope["method"],
                    "path": scope["path"],  # never the query string
                    "status": status,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                }
            },
        )
