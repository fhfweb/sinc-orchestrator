"""
streaming/core/auth.py
======================
FastAPI Dependencies: tenant resolution, API key auth, quota, and logging.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional, Dict, Any

import contextvars
from fastapi import Request, HTTPException, Header, Depends, status

TRACE_CONTEXT: contextvars.ContextVar[str] = contextvars.ContextVar("trace_id", default="")

def get_trace_id() -> str:
    return TRACE_CONTEXT.get()

def set_trace_id(tid: str):
    return TRACE_CONTEXT.set(tid)

from .db import async_db
from .redis_ import async_check_rate_limit, async_incr_token_usage, async_get_token_usage_today

log = logging.getLogger("orchestrator")

_EXPECTED_AUTH_NOISE_PREFIXES = (
    "/health",
    "/metrics",
    "/favicon.ico",
    "/events",
    "/api/v5/dashboard/",
    "/system/infra",
    "/readiness/live",
    "/incidents",
    "/lessons",
    "/events",
    "/queue/poll",
    "/tasks/claim",
)


def _is_expected_auth_noise(path: str) -> bool:
    normalized = str(path or "")
    if normalized.startswith(_EXPECTED_AUTH_NOISE_PREFIXES):
        return True
    return normalized == "/tasks"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _update_last_used_at(key: str) -> None:
    """Fire-and-forget: stamp last_used_at on the api_key row."""
    try:
        async with async_db() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE api_keys SET last_used_at = NOW() WHERE key = %s",
                    (key,),
                )
                await conn.commit()
    except Exception as exc:
        log.debug("last_used_at_update_error error=%s", exc)


# ── Tenant resolution dependency ──────────────────────────────────────────────

from fastapi import Request, HTTPException, Header, Depends, status, Query
...
async def get_tenant_id(
    request: Request,
    x_api_key: Optional[str] = Header(None, alias="X-Api-Key"),
    api_key: Optional[str] = Query(None)
) -> str:
    """
    FastAPI dependency to resolve tenant from X-Api-Key.
    Assigns a correlation_id (UUID) to every request for log tracing.
    Caches the tenant info in request.state for the duration of the request.
    """
    # Assign or propagate a correlation ID for structured logging
    correlation_id = request.headers.get("X-Correlation-ID") or str(uuid.uuid4())
    set_trace_id(correlation_id)
    request.state.correlation_id = correlation_id

    key = x_api_key or api_key
    if not key:
        log_method = log.debug if _is_expected_auth_noise(request.url.path) else log.warning
        log_method("auth_failed: missing_key correlation_id=%s path=%s", correlation_id, request.url.path)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Api-Key header or api_key query param required"
        )

    # 1. Resolve Tenant
    try:
        async with async_db() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT t.id, t.name, t.plan,
                           COALESCE(t.requests_per_minute, 60)   AS requests_per_minute,
                           COALESCE(t.tokens_per_day, 500000)    AS tokens_per_day
                    FROM tenants t
                    WHERE t.api_key = %s
                       OR t.id IN (
                           SELECT tenant_id FROM api_keys
                           WHERE key = %s AND revoked_at IS NULL
                       )
                    LIMIT 1
                    """,
                    (key, key),
                )
                row = await cur.fetchone()
                if not row:
                    log_method = log.debug if _is_expected_auth_noise(request.url.path) else log.warning
                    log_method(
                        "auth_failed: invalid_key key_prefix=%s correlation_id=%s path=%s",
                        key[:8],
                        correlation_id,
                        request.url.path,
                    )
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail="Invalid API Key"
                    )
                
                tenant = dict(row)
                request.state.tenant_id = tenant["id"]
                request.state.tenant = tenant

                # Fire-and-forget: stamp last_used_at without blocking the request
                asyncio.create_task(_update_last_used_at(key))
    except HTTPException:
        raise
    except Exception as e:
        log.exception("auth_service_error error=%s correlation_id=%s", e, correlation_id)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Auth service unavailable"
        )

    # 2. Rate Limiting
    rpm = tenant.get("requests_per_minute", 60)
    if not await async_check_rate_limit(tenant["id"], rpm):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded"
        )

    return tenant["id"]


async def get_tenant(request: Optional[Request] = None) -> Dict[str, Any]:
    """
    Dependency to get full tenant object.
    If called without request (manual call), it will fail gracefully or attempt to resolve.
    """
    if request is None:
        log.warning("get_tenant_called_without_request")
        return {}
        
    if not hasattr(request.state, "tenant"):
        # If get_tenant_id hasn't run, we can't easily resolve here without the full machinery.
        # But we return empty dict to avoid crashing.
        return {}
    return request.state.tenant


# ── Internal helpers ──────────────────────────────────────────────────────────

async def tokens_used_today(tenant_id: str) -> int:
    """
    Return daily token count using Redis (fast path) or DB (fallback).
    """
    cached = await async_get_token_usage_today(tenant_id)
    if cached >= 0:
        return cached

    log.warning("quota_fallback_to_db tenant=%s", tenant_id)
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    SELECT COALESCE(SUM(tokens_in + tokens_out), 0)
                    FROM usage_log
                    WHERE tenant_id = %s
                      AND created_at >= NOW() - INTERVAL '1 day'
                    """,
                    (tenant_id,),
                )
                row = await cur.fetchone()
        return int(list(row.values())[0]) if row else 0
    except Exception:
        return 0


async def check_quota(tenant: dict, tokens_needed: int = 1000) -> None:
    """Assert daily token quota. Raises HTTPException if exceeded."""
    limit = tenant.get("tokens_per_day", 500000)
    used  = await tokens_used_today(tenant.get("id", ""))
    if used + tokens_needed > limit:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail=f"Daily token quota exceeded ({used}/{limit})"
        )


# ── Logging & Webhooks ────────────────────────────────────────────────────────

async def log_usage_async(
    tenant_id: str,
    project_id: str = "",
    endpoint: str = "",
    tier: str = "",
    model: str = "",
    tokens_in: int = 0,
    tokens_out: int = 0,
    latency_ms: int = 0,
    cost_usd: float = 0.0,
) -> None:
    """Async usage logging + Redis counter increment."""
    total_tokens = tokens_in + tokens_out
    if total_tokens > 0:
        await async_incr_token_usage(tenant_id, total_tokens)
    
    try:
        async with async_db() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO usage_log
                        (tenant_id, project_id, endpoint, tier, model,
                         tokens_in, tokens_out, latency_ms, cost_usd)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (tenant_id, project_id, endpoint, tier, model,
                     tokens_in, tokens_out, latency_ms, cost_usd),
                )
                # No commit() needed if we use autocommit or if we just want it done.
                # psycopg AsyncConnection from pool uses autocommit by default in some modes, 
                # but let's be explicit if needed.
                await conn.commit()
    except Exception as exc:
        log.debug("log_usage_error error=%s", exc)


def enqueue_webhook(tenant_id: str, event_type: str, payload: dict):
    # This should be implemented as an async background task in the routes
    pass
