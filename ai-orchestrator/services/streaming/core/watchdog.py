"""
streaming/core/watchdog.py
==========================
Async background task to reclaim 'zombie' tasks (dead agents) and 
manage task retries.
"""
import asyncio
import logging
import json
import os
import time
from datetime import datetime
from .db import async_db
from .config import TASK_MAX_RETRIES, TASK_STALE_TIMEOUT_M
from .runtime_plane import _record_incident_if_needed, ensure_repair_task
from .schema_compat import get_table_columns_cached, get_task_pk_column, insert_agent_event

class TokenBucket:
    """Rate limiter for task reclamation to prevent thundering herds."""
    def __init__(self, capacity: int, fill_rate: float):
        self.capacity = capacity
        self.fill_rate = fill_rate
        self.tokens = float(capacity)
        self.last_update = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self, amount: float = 1.0) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_update
            self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
            self.last_update = now
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False

# Global token bucket for the watchdog
_reclaim_bucket = TokenBucket(capacity=10, fill_rate=2.0)  # 2 reclaims/sec, burst up to 10

# Dynamic import to avoid circular dependencies if any
async def get_event_bus():
    from ...event_bus import get_event_bus as _get
    return await _get()

log = logging.getLogger("orchestrator")

async def run_watchdog():
    """
    Background loop to:
    1. Reclaim 'in-progress' tasks with missing heartbeats.
    2. Reclaim 'delivered' dispatches that never started.
    3. Move tasks to 'dead-letter' after max retries.
    """
    log.info("watchdog_started timeout_m=%d", TASK_STALE_TIMEOUT_M)
    while True:
        try:
            await perform_reclaim_cycle()
            await _drain_llm_retry_queues()
        except Exception as exc:
            log.exception("watchdog_cycle_error error=%s", exc)
        await asyncio.sleep(60)


async def _drain_llm_retry_queues() -> None:
    """Drain LLM retry queues for all active tenants."""
    try:
        from services.streaming.core.redis_ import get_async_redis
        r = get_async_redis()
        if not r:
            return
        keys = await r.keys("sinc:llm_retry:*")
        for key in keys:
            tenant_id = key.split("sinc:llm_retry:", 1)[-1]
            from services.cognitive_orchestrator import process_llm_retry_queue
            requeued = await process_llm_retry_queue(tenant_id)
            if requeued:
                log.info("watchdog_llm_retry_drained tenant=%s count=%d", tenant_id, len(requeued))
    except Exception as exc:
        log.debug("watchdog_llm_retry_drain_error error=%s", exc)

async def perform_reclaim_cycle(task_id: str = None):
    # bypass_rls=True: the watchdog is a background system process that must
    # operate across ALL tenants simultaneously (no per-request tenant context).
    # Row-level security would restrict each query to a single tenant_id, which
    # would make cross-tenant reclaim impossible without iterating every tenant.
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            task_cols = await get_table_columns_cached(cur, "tasks")
            task_pk = await get_task_pk_column(cur)
            heartbeat_cols = await get_table_columns_cached(cur, "heartbeats")
            heartbeat_ts = "beat_at" if "beat_at" in heartbeat_cols else ("updated_at" if "updated_at" in heartbeat_cols else "")
            dispatch_cols = await get_table_columns_cached(cur, "webhook_dispatches")

            task_id_expr = f"t.{task_pk}"
            tenant_expr = "t.tenant_id" if "tenant_id" in task_cols else "''::text"
            project_expr = "t.project_id" if "project_id" in task_cols else "''::text"
            tenant_return_expr = "tenant_id" if "tenant_id" in task_cols else "''::text AS tenant_id"
            project_return_expr = "project_id" if "project_id" in task_cols else "''::text AS project_id"

            # 1. Recover 'in-progress' tasks (Zombies that stopped heartbeating)
            stale_predicate = (
                f"""
                NOT EXISTS (
                    SELECT 1 FROM heartbeats h
                    WHERE h.task_id = {task_id_expr}
                      AND h.{heartbeat_ts} >= NOW() - (%s * INTERVAL '1 minute')
                )
                """
                if heartbeat_ts and "task_id" in heartbeat_cols
                else "t.updated_at < NOW() - (%s * INTERVAL '1 minute')"
            )
            query1 = f"""
                UPDATE tasks t
                SET status = 'pending',
                    assigned_agent = NULL,
                    lock_retry_count = COALESCE(lock_retry_count, 0) + 1,
                    updated_at = NOW()
                WHERE t.status = 'in-progress'
                  AND {stale_predicate}
            """
            params1 = [TASK_STALE_TIMEOUT_M]
            if task_id:
                query1 += f" AND {task_id_expr} = %s"
                params1.append(task_id)
            
            query1 += f" RETURNING {task_id_expr} AS task_id, {tenant_expr} AS tenant_id, {project_expr} AS project_id, assigned_agent, lock_retry_count"
            
            await cur.execute(query1, tuple(params1))
            stale_in_progress = await cur.fetchall()
            for row in stale_in_progress:
                log.warning("watchdog_reclaimed_in_progress task_id=%s agent=%s retries=%d", 
                            row["task_id"], row["assigned_agent"], row["lock_retry_count"])
                await insert_agent_event(
                    cur,
                    task_id=row["task_id"],
                    agent_name="watchdog",
                    tenant_id=row.get("tenant_id") or None,
                    event_type="stale_recovery",
                    payload={"reason": "heartbeat_timeout", "prev_agent": row["assigned_agent"]},
                )
                tenant_id_for_row = row.get("tenant_id") or "local"
                await _record_incident_if_needed(
                    tenant_id=tenant_id_for_row,
                    project_id=row.get("project_id") or "",
                    category="watchdog-stale-recovery",
                    severity="warning",
                    fingerprint=f"watchdog-stale:{tenant_id_for_row}:{row['task_id']}",
                    summary=f"Task {row['task_id']} reclaimed after stale execution",
                    details={"task_id": row["task_id"], "previous_agent": row.get("assigned_agent"), "retries": row.get("lock_retry_count")},
                    task_id=row["task_id"],
                    source="watchdog",
                )
                if int(row.get("lock_retry_count") or 0) >= max(2, TASK_MAX_RETRIES - 1):
                    await ensure_repair_task(
                        tenant_id=tenant_id_for_row,
                        project_id=row.get("project_id") or "",
                        fingerprint=f"watchdog-stale-repair:{tenant_id_for_row}:{row['task_id']}",
                        summary=f"Repair stale task recovery path for {row['task_id']}",
                        details={"source_task_id": row["task_id"], "reason": "repeated_stale_recovery"},
                        source_task_id=row["task_id"],
                        priority=1,
                    )

            # 2. Recover 'delivered' dispatches that never even started heartbeating
            stale_delivery_predicate = (
                f"NOT EXISTS (SELECT 1 FROM heartbeats h WHERE h.task_id = t.{task_pk})"
                if "task_id" in heartbeat_cols
                else "TRUE"
            )
            query2 = f"""
                UPDATE webhook_dispatches wd
                SET status = 'pending',
                    delivered_at = NULL
                FROM tasks t
                WHERE wd.task_id = t.{task_pk}
                  AND wd.status = 'delivered'
                  AND wd.delivered_at < NOW() - (%s * INTERVAL '1 minute')
                  AND {stale_delivery_predicate}
            """
            params2 = [TASK_STALE_TIMEOUT_M]
            if "metadata" in task_cols:
                query2 += """
                  AND NOT (
                        COALESCE(t.metadata->>'external_bridge_enabled', 'false') = 'true'
                        OR LOWER(COALESCE(t.metadata->>'execution_mode', '')) IN ('external-agent', 'manual', 'human')
                  )
                """
            if task_id:
                query2 += f" AND t.{task_pk} = %s"
                params2.append(task_id)
            
            if dispatch_cols:
                query2 += f" RETURNING wd.task_id, wd.agent_name, {tenant_expr} AS tenant_id, {project_expr} AS project_id"
            
            stale_delivered = []
            if dispatch_cols:
                await cur.execute(query2, tuple(params2))
                stale_delivered = await cur.fetchall()
                for row in stale_delivered:
                    log.warning("watchdog_reclaimed_delivered task_id=%s agent=%s", row["task_id"], row["agent_name"])
                    tenant_id_for_row = row.get("tenant_id") or "local"
                    await _record_incident_if_needed(
                        tenant_id=tenant_id_for_row,
                        project_id=row.get("project_id") or "",
                        category="watchdog-delivered-recovery",
                        severity="warning",
                        fingerprint=f"watchdog-delivered:{tenant_id_for_row}:{row['task_id']}",
                        summary=f"Dispatch for task {row['task_id']} was recycled after stale delivery",
                        details={"task_id": row["task_id"], "agent_name": row.get("agent_name")},
                        task_id=row["task_id"],
                        source="watchdog",
                    )
                
            # 3. Dead-letter (Tasks that keep failing/timing out)
            query3 = f"""
                UPDATE tasks
                SET status = 'dead-letter', updated_at = NOW()
                WHERE (status = 'pending' OR status = 'in-progress')
                  AND lock_retry_count >= %s
            """
            params3 = [TASK_MAX_RETRIES]
            if task_id:
                query3 += f" AND {task_pk} = %s"
                params3.append(task_id)
            
            query3 += f" RETURNING {task_pk} AS task_id, {tenant_return_expr}, {project_return_expr}"
            
            await cur.execute(query3, tuple(params3))
            dead = await cur.fetchall()
            for row in dead:
                log.error("watchdog_dead_letter task_id=%s", row["task_id"])
                await insert_agent_event(
                    cur,
                    task_id=row["task_id"],
                    agent_name="watchdog",
                    tenant_id=row.get("tenant_id") or None,
                    event_type="dead_letter",
                    payload={"reason": "max_retries_exceeded", "limit": TASK_MAX_RETRIES},
                )
                tenant_id_for_row = row.get("tenant_id") or "local"
                await _record_incident_if_needed(
                    tenant_id=tenant_id_for_row,
                    project_id=row.get("project_id") or "",
                    category="watchdog-dead-letter",
                    severity="critical",
                    fingerprint=f"watchdog-dead-letter:{tenant_id_for_row}:{row['task_id']}",
                    summary=f"Task {row['task_id']} moved to dead-letter",
                    details={"task_id": row["task_id"], "reason": "max_retries_exceeded", "limit": TASK_MAX_RETRIES},
                    task_id=row["task_id"],
                    source="watchdog",
                )
                await ensure_repair_task(
                    tenant_id=tenant_id_for_row,
                    project_id=row.get("project_id") or "",
                    fingerprint=f"watchdog-dead-letter-repair:{tenant_id_for_row}:{row['task_id']}",
                    summary=f"Repair dead-letter task path for {row['task_id']}",
                    details={"source_task_id": row["task_id"], "reason": "dead_letter"},
                    source_task_id=row["task_id"],
                    priority=0,
                )

            await conn.commit()

    # 4. Redis Stream PEL (Pending Entity List) recovery
    try:
        bus = await get_event_bus()
        stream_name = "stream:orch:task_pool"
        group_name  = "agent_workers"
        consumer_id = "watchdog-reaper"
        
        # min_idle_time = TASK_STALE_TIMEOUT_M minutes
        idle_ms = TASK_STALE_TIMEOUT_M * 60 * 1000
        
        # XAUTOCLAIM will move stale pending messages to the watchdog-reaper
        # Apply backpressure using the TokenBucket
        if await _reclaim_bucket.consume(1.0):
            next_id, claimed, deleted = await bus.auto_claim(
                stream_name, group_name, consumer_id, min_idle_time_ms=idle_ms
            )
            if claimed:
                log.info("watchdog_stream_autoclaim_count count=%d", len(claimed))
                for msg_id, data in claimed:
                    payload = json.loads(data.get("data", "{}"))
                    log.warning("watchdog_re-dispatching_stream_task task_id=%s msg_id=%s", 
                                payload.get("id"), msg_id)
                    
                    # Push back to pool (without id to generate new stream id)
                    await bus.publish("orch:task_pool", payload, use_stream=True)
                    
                    # ACK the old one so it leaves the PEL
                    await bus.ack(stream_name, group_name, msg_id)
        else:
            log.debug("watchdog_reclaim_throttled waiting_for_tokens")
                
    except Exception as e:
        log.error("watchdog_stream_reclaim_error error=%s", e)
