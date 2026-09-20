"""Errors raised by the gateway itself.

Upstream errors are passed through untouched, in Google's shape:
    {"error": {"code": 429, "message": "...", "status": "RESOURCE_EXHAUSTED"}}

Gateway errors carry "source": "gateway" and a snake_case code instead, so a caller
can tell who failed from the body alone:
    {"error": {"source": "gateway", "code": "invalid_api_key", "message": "..."}}
"""

from fastapi import Request
from fastapi.responses import JSONResponse


class GatewayError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        # Some refusals carry a hint the body cannot: Retry-After on a 429 is the one
        # header a client's own retry logic already knows how to read.
        self.headers = headers


async def handle_gateway_error(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, GatewayError):
        raise exc
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"source": "gateway", "code": exc.code, "message": exc.message}},
        headers=exc.headers,
    )
