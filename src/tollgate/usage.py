"""Usage accounting: reading token counts from responses and writing the ledger."""

import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import UsageRecord


def apply_usage_metadata(record: UsageRecord, body: bytes) -> None:
    """Copy token counts from a Gemini response body onto the record.

    Leaves them NULL if the body isn't JSON or has no usageMetadata, so a malformed
    response is recorded as "unknown" rather than as zero tokens.
    """
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return
    usage = payload.get("usageMetadata") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return
    record.input_tokens = usage.get("promptTokenCount")
    record.output_tokens = usage.get("candidatesTokenCount")
    record.thoughts_tokens = usage.get("thoughtsTokenCount")


def upstream_error_code(body: bytes) -> str | None:
    """The `status` field of a Google-shaped error body, e.g. RESOURCE_EXHAUSTED."""
    try:
        payload: Any = json.loads(body)
        status = payload["error"]["status"]
    except (ValueError, KeyError, TypeError):
        return None
    return status if isinstance(status, str) else None


async def write_usage(sessionmaker: async_sessionmaker[AsyncSession], record: UsageRecord) -> None:
    async with sessionmaker() as session:
        session.add(record)
        await session.commit()
