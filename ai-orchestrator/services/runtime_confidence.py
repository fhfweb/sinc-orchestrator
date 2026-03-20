"""
runtime_confidence.py
=====================
Runtime confidence monitoring and proactive recovery (Nível 5).
Detects stagnation, error spikes, and stale heartbeats during active execution.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Optional, Any
from uuid import uuid4

log = logging.getLogger("orchestrator.runtime.confidence")

async def _task_pk_column(cur) -> str:
    from services.streaming.core.schema_compat import get_table_columns_cached
    cols = await get_table_columns_cached(cur, "tasks")
    return "task_id" if "task_id" in cols else "id"

async def _heartbeat_time_column(cur) -> str:
    from services.streaming.core.schema_compat import get_table_columns_cached
    cols = await get_table_columns_cached(cur, "heartbeats")
    return "beat_at" if "beat_at" in cols else "updated_at"

async def monitor_runtime_confidence(task_id: str, tenant_id: str):
    """
    Background monitor for a running task.
    Detects degradation and triggers handle_confidence_drop.
    """
    log.info(f"RUNTIME_MONITOR_START task={task_id}")
    
    from services.streaming.core.db import get_async_pool
    from services.streaming.core.redis_ import get_async_redis
    pool = get_async_pool()
    r = get_async_redis()
    
    last_progress = 0.0
    stagnant_count = 0
    
    while True:
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    task_pk = await _task_pk_column(cur)
                    heartbeat_time_col = await _heartbeat_time_column(cur)
                    
                    # 1. Check task status
                    await cur.execute(
                        f"SELECT status, current_progress, assigned_agent FROM tasks WHERE {task_pk} = %s",
                        (task_id,),
                    )
                    task = await cur.fetchone() # Assumes DictCursor or similar if mapped, else tuple
                    
                    # Psycopg Default Cursor returns tuple if not configured
                    if not task: break
                    
                    # Simple mapping for robustness
                    status = task[0] if isinstance(task, (tuple, list)) else task.get("status")
                    curr_prog = task[1] if isinstance(task, (tuple, list)) else task.get("current_progress")
                    
                    if status != "in-progress":
                        break
                    
                    # 2. Check Heartbeat (Stale agent)
                    await cur.execute(
                        f"SELECT MAX({heartbeat_time_col}) FROM heartbeats WHERE task_id = %s",
                        (task_id,),
                    )
                    last_beat = await cur.scalar()
                    
                    if last_beat:
                        if isinstance(last_beat, str):
                            last_beat = datetime.fromisoformat(last_beat.replace("Z", "+00:00"))
                        
                        now = datetime.now(timezone.utc)
                        if last_beat.tzinfo is None: last_beat = last_beat.replace(tzinfo=timezone.utc)
                        
                        seconds_since = (now - last_beat).total_seconds()
                        if seconds_since > 120:
                            await _handle_drop(cur, r, task_id, tenant_id, "stale_heartbeat", "check_agent_alive")
                    
                    # 3. Check Stagnation
                    progress = float(curr_prog or 0.0)
                    if progress <= last_progress + 0.01:
                        stagnant_count += 1
                    else:
                        stagnant_count = 0
                    
                    last_progress = progress
                    
                    if stagnant_count >= 5: # ~2.5 mins of stagnation
                        await _handle_drop(cur, r, task_id, tenant_id, "progress_stagnation", "inject_hint_or_reassign")
                        stagnant_count = 0 

                    # 4. Check Recent Error Spikes
                    await cur.execute("""
                        SELECT COUNT(*) FROM agent_events 
                        WHERE task_id = %s AND event_type = 'error' 
                          AND created_at > NOW() - INTERVAL '5 minutes'
                    """, (task_id,))
                    error_count = await cur.scalar() or 0
                    
                    if error_count >= 3:
                        await _handle_drop(cur, r, task_id, tenant_id, "error_spike", "switch_agent_or_decompose")

            await asyncio.sleep(30)
        except Exception as e:
            log.error(f"runtime_monitor_error: {e}")
            await asyncio.sleep(60)

async def _handle_drop(cur: Any, r: Any, task_id: str, tenant_id: str, trigger: str, action: str):
    log.warning(f"CONFIDENCE_DROP task={task_id} trigger={trigger} action={action}")
    
    # Audit event
    try:
        await cur.execute("""
            INSERT INTO runtime_confidence_events (task_id, tenant_id, trigger, action_taken)
            VALUES (%s, %s, %s, %s)
        """, (task_id, tenant_id, trigger, action))
    except: pass

    # Execute Action
    if action == "inject_hint_or_reassign":
        hint = "System detects stagnation. Suggesting check of prerequisites or logs."
        await inject_runtime_hint(task_id, hint, tenant_id, r)
    elif action in ["switch_agent_or_decompose", "reassign_task"]:
        log.warning(f"RUNTIME_INTERVENTION task={task_id} action={action} due to {trigger}")

async def inject_runtime_hint(task_id: str, hint: str, tenant_id: str, r: Any):
    """
    Injeta uma dica no agente via Redis. (Módulo 4.1)
    """
    if not r: return
    hint_key = f"sinc:runtime_hint:{task_id}"
    await r.setex(hint_key, 300, json.dumps({
        "hint": hint,
        "source": "runtime_confidence_monitor",
        "injected_at": datetime.now(timezone.utc).isoformat()
    }))
    log.info(f"RUNTIME_HINT_INJECTED task={task_id}")
