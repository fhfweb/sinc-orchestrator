"""
execution_router.py
===================
High-performance routing for SINC AI Engineering System (Nível 5).
Decides between INSTANT, FAST, STANDARD, and DEEP paths in <50ms.
"""
import json
import logging
from enum import Enum
from typing import Tuple, Any, Optional
from services.streaming.core.redis_ import get_async_redis

log = logging.getLogger("orchestrator.router")

class ExecutionPath(Enum):
    INSTANT  = "instant"   # L0 cache hit -> <50ms
    FAST     = "fast"      # Simplified confidence, no simulation -> <300ms
    STANDARD = "standard"  # Full GCS, no simulation -> <800ms
    DEEP     = "deep"      # Full GCS + Simulation -> <2000ms

LOW_RISK_CATEGORIES = ["docs", "formatting", "boilerplate", "read_only"]
HIGH_RISK_CATEGORIES = ["security", "database_migration", "refactor", "deployment"]

async def route_execution(
    task: Any,
    tenant_id: str
) -> Tuple[ExecutionPath, str]:
    """
    Tiered routing based on intent, risk, and cache.
    All signals here must be cached (Redis) for <50ms decision.
    """
    r = get_async_redis()
    task_title = getattr(task, 'title', 'none')
    task_category = getattr(task, 'category', 'generic')
    
    # 1. INSTANT: L0 Cache hit
    cache_key = f"l0:{tenant_id}:{hash(task_title)}"
    if await r.exists(cache_key):
        return ExecutionPath.INSTANT, "l0_cache_hit"

    # 2. Redis Sinais
    category_stats_raw = await r.get(f"sinc:category_stats:{tenant_id}:{task_category}")
    agent_score_raw = await r.zscore(
        f"sinc:leaderboard:{tenant_id}:{task_category}",
        getattr(task, 'assigned_agent', 'none')
    )
    recent_failures = int(await r.get(f"sinc:recent_failures:{tenant_id}:{task_category}") or 0)

    category_stats = json.loads(category_stats_raw) if category_stats_raw else {}
    agent_score = float(agent_score_raw or 0.5)

    # FAST PATH Signals
    fast_signals = [
        category_stats.get("success_rate", 0) >= 0.80,   # categoria tem bom histórico
        agent_score >= 0.75,                               # agente está performando bem
        recent_failures < 2,                               # sem falhas recentes
        task_category in LOW_RISK_CATEGORIES,              # categoria de baixo risco
        not getattr(task, 'primary_file', None),           # não envolve arquivo específico
    ]

    # DEEP PATH Signals
    deep_signals = [
        category_stats.get("success_rate", 1) < 0.50,     # categoria problemática
        recent_failures >= 3,                              # padrão de falha recente
        task_category in HIGH_RISK_CATEGORIES,             # categoria de alto risco
        len(getattr(task, 'dependencies', [])) > 5,        # muitas dependências
    ]

    if sum(deep_signals) >= 2:
        return ExecutionPath.DEEP, f"{sum(deep_signals)} alto risco detectados"
    
    if sum(fast_signals) >= 3:
        return ExecutionPath.FAST, f"{sum(fast_signals)}/5 sinais favoráveis"

    return ExecutionPath.STANDARD, "Standard execution profile"

async def dispatch_with_routing(task: Any, tenant_id: str):
    """
    Entry point for Level 5 execution.
    """
    path, reason = await route_execution(task, tenant_id)
    log.info(f"ROUTING_DECISION: path={path.value} task={getattr(task, 'id', 'none')} reason={reason}")

    # Persist for analytics
    try:
        from services.streaming.core.db import get_async_pool
        pool = get_async_pool()
        async with pool.connection() as conn:
            await conn.execute("""
                INSERT INTO execution_routes (task_id, tenant_id, execution_path, reason)
                VALUES (%s, %s, %s, %s)
            """, (getattr(task, 'id', None), tenant_id, path.value, reason))

    except: pass

    # Execution dispatch
    from services.cognitive_graph import get_cognitive_graph, DEFAULT_MAX_STEPS
    graph = get_cognitive_graph()
    
    # We pass the path to the graph so nodes can skip logic if FAST/INSTANT
    initial_state = {
        "task": task if isinstance(task, dict) else (task.model_dump() if hasattr(task, "model_dump") else task.dict()),
        "task_type": getattr(task, 'category', 'generic'),
        "tenant_id": tenant_id,
        "execution_path": path.value,
        "max_steps": DEFAULT_MAX_STEPS
    }
    
    # Sprint 4: Start Runtime Confidence Monitor for high-risk paths
    if path.value in ["standard", "deep"]:
        from services.runtime_confidence import monitor_runtime_confidence
        asyncio.create_task(monitor_runtime_confidence(getattr(task, 'id', None), tenant_id))

    return await graph.ainvoke(initial_state)
