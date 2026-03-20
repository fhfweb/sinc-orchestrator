import asyncio
import logging
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncGenerator, Optional
from services.streaming.core.db import async_db
from services.streaming.core.redis_ import get_async_redis
from services.streaming.core.schema_compat import get_task_pk_column
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator.runtime_confidence")

@dataclass
class ConfidenceDrop:
    task_id: str
    from_level: str
    to_level: str
    trigger: str
    recommended_action: str

async def monitor_runtime_confidence(task_id: str, tenant_id: str) -> AsyncGenerator[ConfidenceDrop, None]:
    """
    Monitora sinais de degradação em runtime.
    """
    stale_warnings = 0
    
    while True:
        await asyncio.sleep(15)
        
        async with async_db(tenant_id=tenant_id) as conn:
            # Pegar status atual e último heartbeat
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
            res = await conn.execute(
                "SELECT status, updated_at FROM tasks WHERE {task_pk} = %s".format(task_pk=task_pk),
                (task_id,),
            )
            task = await res.fetchone()
            
            if not task or task["status"] != "in-progress":
                break

            # Sinal 1: Heartbeat atrasado (> 2 min)
            res = await conn.execute("SELECT MAX(created_at) FROM agent_events WHERE task_id = %s", (task_id,))
            last_event = await res.fetchone()
            
            if last_event and last_event[0]:
                now = datetime.now(timezone.utc)
                diff = (now - last_event[0]).total_seconds()
                if diff > 120:
                    stale_warnings += 1
                    if stale_warnings >= 2:
                        yield ConfidenceDrop(
                            task_id=task_id,
                            from_level="medium",
                            to_level="critical_low",
                            trigger=f"sem eventos por {diff:.0f}s",
                            recommended_action="check_agent_alive"
                        )

        # Sinal 2: Progresso estagnado (via Redis progress_pct se disponível)
        # (Simplificado para este ambiente)
        pass

async def handle_confidence_drop(drop: ConfidenceDrop, tenant_id: str):
    log.warning(f"confidence_drop task={drop.task_id} trigger={drop.trigger} action={drop.recommended_action}")
    
    await broadcast("confidence_alert", {
        "task_id": drop.task_id,
        "trigger": drop.trigger,
        "action": drop.recommended_action,
        "level": drop.to_level
    }, tenant_id=tenant_id)
    if drop.recommended_action == "check_agent_alive":
        # Injetar hint de verificação via Redis
        redis = get_async_redis()
        if redis:
            hint_key = f"sinc:runtime_hint:{drop.task_id}"
            await redis.setex(hint_key, 300, json.dumps({
                "hint": "VERIFICAÇÃO DE STATUS: Por favor confirme que ainda está operando.",
                "type": "ping",
                "source": "runtime_confidence_monitor"
            }))

    # Registrar evento
    async with async_db(tenant_id=tenant_id) as conn:
        await conn.execute("""
            INSERT INTO runtime_confidence_events (task_id, tenant_id, from_level, to_level, trigger, action_taken)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (drop.task_id, tenant_id, drop.from_level, drop.to_level, drop.trigger, drop.recommended_action))
        await conn.commit()
