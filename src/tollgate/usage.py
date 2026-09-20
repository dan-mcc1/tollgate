"""Usage accounting: reading token counts from responses and writing the ledger."""

import asyncio
import json
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import UsageRecord

logger = logging.getLogger("tollgate.usage")

# Ledger writes that outlived the request that started them (a client that hung up
# mid-stream). Kept referenced so the event loop can't garbage-collect them mid-write.
_background_writes: set[asyncio.Task[None]] = set()


def apply_usage_payload(record: UsageRecord, payload: dict[str, Any]) -> None:
    """Copy token counts from one parsed Gemini response (or stream event) onto the record.

    A streamed response repeats usageMetadata on each event with running totals, so the
    last event seen wins. Counts stay NULL when the upstream never reported any, which is
    different from reporting zero.
    """
    usage = payload.get("usageMetadata")
    if not isinstance(usage, dict):
        return
    record.input_tokens = usage.get("promptTokenCount")
    record.output_tokens = usage.get("candidatesTokenCount")
    record.thoughts_tokens = usage.get("thoughtsTokenCount")


def apply_usage_metadata(record: UsageRecord, body: bytes) -> None:
    """As above, for a complete (non-streamed) response body."""
    try:
        payload: Any = json.loads(body)
    except ValueError:
        return
    if isinstance(payload, dict):
        apply_usage_payload(record, payload)


def upstream_error_status(payload: dict[str, Any]) -> str | None:
    """The error status inside one parsed response or stream event, if it carries one.

    A provider can fail *inside* a stream: the connection stays healthy and an ordinary
    event arrives holding an error object instead of content. Nothing else notices, so
    without this the ledger would record a failed request as a clean success.
    """
    error = payload.get("error")
    if not isinstance(error, dict):
        return None
    status = error.get("status") or error.get("code")
    return str(status) if status is not None else "unknown"


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


async def write_usage_even_if_cancelled(
    sessionmaker: async_sessionmaker[AsyncSession], record: UsageRecord
) -> None:
    """Write the ledger row even when the caller is being cancelled.

    A client hanging up mid-stream cancels the task doing the relaying, and a plain
    `await` inside that task would be cancelled too, losing a row for tokens the provider
    has already generated and billed. The write runs as its own task, shielded, so
    cancellation stops the waiting, not the writing.
    """
    task = asyncio.create_task(write_usage(sessionmaker, record))
    _background_writes.add(task)
    task.add_done_callback(_finish_background_write)
    try:
        await asyncio.shield(task)
    except asyncio.CancelledError:
        pass  # the shielded task keeps running; the caller's cancellation continues below
    except Exception:
        logger.exception("failed to write usage row")


def _finish_background_write(task: asyncio.Task[None]) -> None:
    _background_writes.discard(task)
    if not task.cancelled() and (error := task.exception()) is not None:
        logger.error("usage row lost", exc_info=error)
