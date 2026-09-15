"""Structured JSON logs, each line tagged with the request it belongs to.

Every request gets an id: the caller's `X-Request-ID` if it's well formed, otherwise a new
one. The id is echoed back in the response and added to every log line written while the
request is handled, so one search in CloudWatch shows everything that happened to it.

Rule: never log prompt or response text, headers, or query strings (a tenant may put its
key in `?key=`). Log metadata only.
"""

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
tenant_var: ContextVar[str | None] = ContextVar("tenant", default=None)

REQUEST_ID_HEADER = "x-request-id"
VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Load balancer health checks arrive every few seconds; logging them buries real traffic.
UNLOGGED_PATHS = frozenset({"/livez", "/readyz"})

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
    """Assigns the request id, returns it as a header, and writes one access log line.

    A plain ASGI middleware rather than Starlette's BaseHTTPMiddleware, so it passes
    streaming responses through untouched (phase 3 depends on that).
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
        started = time.perf_counter()
        status_code = 500  # if the app raises before responding, that's what the caller sees

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            if scope["path"] not in UNLOGGED_PATHS:
                access_logger.info(
                    "request",
                    extra={
                        "fields": {
                            "request_id": request_id,
                            "tenant": tenant_var.get(),
                            "method": scope["method"],
                            "path": scope["path"],  # never the query string
                            "status": status_code,
                            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                        }
                    },
                )
            tenant_var.reset(tenant_token)
            request_id_var.reset(request_token)
