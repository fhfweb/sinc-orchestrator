"""
streaming/routes/events.py
==========================
GET  /events                    — SSE stream (Redis-backed)
POST /events                    — push one or more events into the Redis bus
GET  /tasks/{task_id}/events    — full event + heartbeat history for a task
"""
import logging
import uuid
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Header
from sse_starlette.sse import EventSourceResponse

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db
from services.streaming.core.schema_compat import get_task_pk_column, get_table_columns_cached
from services.streaming.core.sse import (
    get_event_iterator, broadcast,
    connection_register, connection_unregister, connection_count,
    connection_limit,
)

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["events"])


async def _table_has_tenant(cur, table_name: str) -> bool:
    return "tenant_id" in await get_table_columns_cached(cur, table_name)


async def _resolve_tenant_id() -> str:
    return await get_tenant_id()


async def _resolve_tenant() -> dict:
    return await get_tenant()

@router.get("/events")
async def stream_events(
    last_event_id: str = Header(None, alias="Last-Event-ID"),
    tenant_id: str = Depends(_resolve_tenant_id),
    tenant: dict = Depends(_resolve_tenant),
):
    """
    Server-Sent Events stream — Redis-backed and cluster-safe.
    Supports replay via Last-Event-ID. Enforces per-tenant connection limits.
    """
    plan    = tenant.get("plan", "free")
    limit   = connection_limit(plan)
    current = await connection_count(tenant_id)
    if current >= limit:
        raise HTTPException(
            status_code=429,
            detail=f"SSE connection limit reached ({current}/{limit}) for plan '{plan}'"
        )

    conn_id  = str(uuid.uuid4())
    start_id = last_event_id or "$"
    log.info("sse_client_connected tenant=%s conn_id=%s last_id=%s", tenant_id, conn_id, last_event_id)

    async def _tracked():
        await connection_register(tenant_id, conn_id)
        try:
            async for event in get_event_iterator(tenant_id, last_id=start_id):
                yield event
        finally:
            await connection_unregister(tenant_id, conn_id)
            log.info("sse_client_disconnected tenant=%s conn_id=%s", tenant_id, conn_id)

    return EventSourceResponse(_tracked())


@router.post("/events")
async def push_events(
    body: Dict[str, Any],
    tenant_id: str = Depends(_resolve_tenant_id)
):
    """
    Push one or more SSE events via Redis.
    """
    events = body.get("events") or [body]
    pushed = 0
    for evt in events:
        etype = evt.get("type") or evt.get("event_type", "client_event")
        data  = evt.get("data") or {k: v for k, v in evt.items() 
                                    if k not in ("type", "event_type")}
        await broadcast(etype, data, tenant_id=tenant_id)
        pushed += 1

    return {"ok": True, "pushed": pushed}


@router.get("/tasks/{task_id}/events")
async def get_task_events(
    task_id: str,
    tenant_id: str = Depends(_resolve_tenant_id)
):
    """Full event history for a task."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                tasks_has_tenant = await _table_has_tenant(cur, "tasks")
                # 1. Verify ownership
                await cur.execute(
                    "SELECT "
                    + task_pk
                    + " FROM tasks WHERE "
                    + f"{task_pk} = %s"
                    + (" AND tenant_id = %s" if tasks_has_tenant else ""),
                    (task_id, tenant_id) if tasks_has_tenant else (task_id,),
                )
                if not await cur.fetchone():
                    raise HTTPException(status_code=404, detail="Task not found")

                # 2. History
                await cur.execute(
                    """
                    SELECT event_type, actor, payload, created_at
                    FROM agent_events
                    WHERE task_id = %s
                    ORDER BY created_at ASC
                    """,
                    (task_id,),
                )
                events = await cur.fetchall()

                # 3. Heartbeats
                await cur.execute(
                    """
                    SELECT agent_name, beat_at, progress_pct, current_step
                    FROM heartbeats WHERE task_id = %s
                    ORDER BY beat_at ASC
                    """,
                    (task_id,),
                )
                heartbeats = await cur.fetchall()

        return {
            "task_id":    task_id,
            "events":     events,
            "heartbeats": heartbeats,
        }
    except HTTPException:
        raise
    except Exception as e:
        log.exception("get_task_events_error task_id=%s", task_id)
        return {"error": str(e)}
