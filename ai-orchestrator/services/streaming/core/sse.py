"""
streaming/core/sse.py
=====================
Tenant-isolated SSE broker with Redis Pub/Sub backend.
Connection tracking uses Redis SETs (SADD/SCARD/SREM) per tenant
so the count is accurate across multiple server instances.
"""

import logging
from datetime import datetime, timezone
from typing import AsyncGenerator

from .billing import SSE_LIMITS, SSE_DEFAULT_LIMIT
from .redis_ import get_async_redis

log = logging.getLogger("orchestrator")

# Redis channel names
_CHANNEL_PREFIX   = "sse:"
_ALL_CHANNEL      = "sse:_all"
_ORCH_CHANNEL     = "orch:events"
_CONN_PREFIX      = "sse_conns:"
_CONN_TTL_SECONDS = 3600  # Safety expiry in case SREM is never called


async def get_event_iterator(tenant_id: str, last_id: str = "$") -> AsyncGenerator[str, None]:
    """
    Returns an async generator that yields events from Redis Streams.
    Supports replay from last_id.
    """
    from services.event_bus import EventBus
    bus = await EventBus.get_instance()
    await bus.connect()
    
    # Listen to tenant-specific stream
    channel = f"{_CHANNEL_PREFIX}{tenant_id}" if tenant_id else _ALL_CHANNEL
    
    async for message in bus.get_xstream_iterator(channel, last_id=last_id):
        yield message


async def broadcast(event_type: str, data: dict, tenant_id: str = "") -> None:
    """
    Push an SSE event to Redis Streams + Pub/Sub via EventBus.
    """
    from services.event_bus import EventBus
    bus = await EventBus.get_instance()
    await bus.connect()
    
    from .auth import get_trace_id
    payload = {
        "type":      event_type,
        "data":      data,
        "tenant_id": tenant_id,
        "trace_id":  get_trace_id(),
        "ts":        datetime.now(timezone.utc).isoformat(),
    }
    
    channel = f"{_CHANNEL_PREFIX}{tenant_id}" if tenant_id else _ALL_CHANNEL
    try:
        await bus.publish(channel, payload, use_stream=True)
        # Also notify the internal orchestrator bus for immediate worker/scheduler reaction
        await bus.publish(_ORCH_CHANNEL, payload, use_stream=False)
    except Exception as exc:
        log.debug("sse_eventbus_publish_error error=%s", exc)


def connection_limit(plan: str) -> int:
    return SSE_LIMITS.get(plan, SSE_DEFAULT_LIMIT)


async def connection_register(tenant_id: str, conn_id: str) -> None:
    """Add conn_id to the tenant's active-connection SET in Redis."""
    r = get_async_redis()
    if not r:
        return
    key = f"{_CONN_PREFIX}{tenant_id}"
    try:
        async with r.pipeline(transaction=True) as pipe:
            await pipe.sadd(key, conn_id)
            await pipe.expire(key, _CONN_TTL_SECONDS)
            await pipe.execute()
    except Exception as exc:
        log.debug("sse_register_error conn_id=%s error=%s", conn_id, exc)


async def connection_unregister(tenant_id: str, conn_id: str) -> None:
    """Remove conn_id from the tenant's active-connection SET in Redis."""
    r = get_async_redis()
    if not r:
        return
    key = f"{_CONN_PREFIX}{tenant_id}"
    try:
        await r.srem(key, conn_id)
    except Exception as exc:
        log.debug("sse_unregister_error conn_id=%s error=%s", conn_id, exc)


async def connection_count(tenant_id: str) -> int:
    """Return the live SSE connection count for a tenant from Redis (cluster-safe)."""
    r = get_async_redis()
    if not r:
        return 0
    key = f"{_CONN_PREFIX}{tenant_id}"
    try:
        return await r.scard(key)
    except Exception:
        return 0
