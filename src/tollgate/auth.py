"""Tenant authentication.

Tenants send a Tollgate key in the same place the Gemini SDK sends a Google key
(the `x-goog-api-key` header, or `?key=`), so the SDK works unchanged.

Only a SHA-256 hash of each key is stored. A fast hash is enough: keys are 256
random bits, so there is nothing to guess, unlike a human-chosen password. A slow
password hash (bcrypt, argon2) would add ~100 ms to every request for no gain.
"""

import hashlib
import secrets
import uuid
from dataclasses import dataclass

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tollgate.db.models import ApiKey, Tenant
from tollgate.errors import GatewayError
from tollgate.logs import tenant_var

KEY_PREFIX = "tg_"
STORED_PREFIX_LENGTH = 12  # enough to recognise a key in logs, far too little to use it


@dataclass(frozen=True)
class TenantContext:
    tenant_id: uuid.UUID
    tenant_name: str
    api_key_id: uuid.UUID


@dataclass(frozen=True)
class NewKey:
    plaintext: str  # shown to the tenant once, never stored
    prefix: str
    hash: str


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_key() -> NewKey:
    plaintext = KEY_PREFIX + secrets.token_urlsafe(32)
    return NewKey(plaintext, plaintext[:STORED_PREFIX_LENGTH], hash_key(plaintext))


def extract_key(request: Request) -> str | None:
    return request.headers.get("x-goog-api-key") or request.query_params.get("key")


async def resolve_key(session: AsyncSession, key: str) -> TenantContext | None:
    """Return the tenant for a live key, or None if the key is unknown, revoked, or
    belongs to a deactivated tenant."""
    stmt = (
        select(ApiKey.id, Tenant.id, Tenant.name)
        .join(Tenant, ApiKey.tenant_id == Tenant.id)
        .where(
            ApiKey.key_hash == hash_key(key),
            ApiKey.revoked_at.is_(None),
            Tenant.is_active.is_(True),
        )
    )
    row = (await session.execute(stmt)).one_or_none()
    if row is None:
        return None
    api_key_id, tenant_id, tenant_name = row
    return TenantContext(tenant_id=tenant_id, tenant_name=tenant_name, api_key_id=api_key_id)


async def require_tenant(request: Request) -> TenantContext:
    """FastAPI dependency: authenticate the caller or raise a 401."""
    key = extract_key(request)
    if not key:
        raise GatewayError(401, "no_key_sent", "Send a Tollgate key in x-goog-api-key.")

    sessionmaker: async_sessionmaker[AsyncSession] = request.app.state.sessionmaker
    async with sessionmaker() as session:
        tenant = await resolve_key(session, key)

    if tenant is None:
        # Unknown and revoked get the same answer, so a caller can't probe which keys once existed.
        raise GatewayError(401, "invalid_api_key", "API key is unknown or revoked.")
    tenant_var.set(tenant.tenant_name)  # tags this request's log lines with the tenant
    return tenant
