"""
realtime_learning.py
=====================
High-speed knowledge reinforcement loop.
Updates L0/L1 caches immediately as per prompt Módulo 5.1.
"""
import logging
import asyncio
from typing import Any, Dict

log = logging.getLogger("orchestrator.learning")

IMMEDIATE_LEARNING_THRESHOLD = 0.90

async def process_lesson_with_realtime_update(state: Dict, succeeded: bool, error: Any, tenant_id: str):
    """
    Adapts system immediately when lesson has high confidence (Módulo 5.1).
    """
    confidence = state.get("confidence_score", 0.0)
    task = state.get("task", {})
    agent_name = state.get("planner_name", "orchestrator")
    task_category = state.get("task_type", "generic")

    if confidence >= IMMEDIATE_LEARNING_THRESHOLD:
        # 1. Update Redis Leaderboard
        from services.streaming.core.redis_ import get_async_redis
        r = await get_async_redis()
        # Simulated leaderboard update
        await r.zincrby(f"sinc:leaderboard:{tenant_id}:{task_category}", 1.0 if succeeded else -1.0, agent_name)
        
        # 2. L0 Promotion
        if succeeded:
            cache_key = f"l0:{tenant_id}:{hash(task.get('title',''))}"
            await r.setex(cache_key, 172800, "promoted_success")
            log.info(f"REALTIME_L0_PROMOTION: category={task_category} confidence={confidence:.2f}")

        # 3. Emit SSE Event
        log.info(f"SYSTEM_LEARNED: tenant={tenant_id} category={task_category}")
