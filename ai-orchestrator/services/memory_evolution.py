import logging
import json
import asyncio
import hashlib
from datetime import datetime
from typing import Dict, Any, List, Optional

from services.background_tasks import get_background_task_registry
from uuid import uuid4
from services.evolutionary_distillation import get_distillation_service

log = logging.getLogger("orch.memory.evolution")


async def _maybe_schedule_distillation(
    tenant_id: str,
    *,
    verified: bool,
    redis_client,
    threshold: int = 10,
) -> bool:
    if not verified or redis_client is None:
        return False

    v_count_key = f"sinc:verified_count:{tenant_id}"
    v_count = await redis_client.incr(v_count_key)
    if v_count < threshold:
        return False

    registry = get_background_task_registry()
    owner = f"memory_distillation:{tenant_id}"
    if registry.has_live_tasks(owner):
        log.info("distillation_already_running tenant=%s", tenant_id)
        return False

    distiller = get_distillation_service()
    registry.spawn(
        owner,
        distiller.extract_verified_traces(tenant_id),
        name=f"memory.distillation:{tenant_id}",
    )
    await redis_client.set(v_count_key, 0)
    return True

async def generate_and_store_lesson(
    state: Dict[str, Any],
    solution: str,
    succeeded: bool,
    error: Optional[str] = None,
    verified: bool = False
) -> str:
    """
    Module 3.1: Extracts a lesson from execution and stores it across the stack.
    """
    from services.llm_solver import solve as _llm_solve # Re-using LLM solver for lesson extraction

    from services.context_retriever import ContextRetriever
    from services.streaming.core.db import get_async_pool

    from services.streaming.core.redis_ import get_async_redis
    
    r = get_async_redis()
    task_id = state["task"].get("id", "unknown")
    tenant_id = state["tenant_id"]
    project_id = state["project_id"]
    description = state["description"]
    task_type = state["task_type"]
    planner = state.get("planner_name", "unknown")
    confidence = state.get("confidence", 0.0)
    validation_gate = state.get("validation_gate") or {}
    
    # 1. LLM Lesson Extraction
    lesson_text = "Task completed successfully using standard patterns."
    try:
        lesson_prompt = f"""
        Analyze this execution and extract ONE concise lesson (max 2 sentences) for future similar tasks.
        Task: {description}
        Category: {task_type}
        Result: {'SUCCESS' if succeeded else 'FAILURE'}
        Error: {error or 'None'}
        Planner: {planner}
        """
        resp = await _llm_solve(
            description=lesson_prompt,
            task_type="lesson_extraction",
            steps=[],
            hint="Extract a concise technical lesson.",
            tenant_id=tenant_id
        )
        lesson_text = resp.solution
    except Exception as e:
        log.warning("lesson_extraction_failed error=%s", e)



    # 2. Qdrant Storage (Memory L2)
    if succeeded:
        try:
            cr = ContextRetriever()
            await asyncio.to_thread(
                cr.store_solution,
                description, f"Lesson: {lesson_text}\nSolution: {solution}", project_id, tenant_id,
                intent=task_type,
                verified=verified,
                metadata={
                    "task_id": task_id,
                    "task_type": task_type,
                    "planner_name": planner,
                    "validation_gate": validation_gate,
                },
            )
        except Exception as e:
            log.warning("qdrant_lesson_store_failed error=%s", e)

    # 3. Neo4j relationship updates (Structural Memory)
    try:
        from neo4j import GraphDatabase
        from services.context_retriever import NEO4J_URI, NEO4J_USER, NEO4J_PASS
        auth = (NEO4J_USER, NEO4J_PASS)
        rel = "SUCCEEDED_ON" if succeeded else "FAILED_ON"
        def _neo4j_learn():
            with GraphDatabase.driver(NEO4J_URI, auth=auth) as driver:
                with driver.session() as s:
                    s.run("""
                        MERGE (t:Task {id: $tid, tenant_id: $tenant_id})
                        SET t.status = $status
                        MERGE (a:Agent {name: $p, tenant_id: $tenant_id})
                        MERGE (l:Lesson {id: $lid})
                        SET l.text = $text, l.created_at = datetime(), l.tenant_id = $tenant_id, l.applicable_categories = $cats
                        MERGE (t)-[:GENERATED_LESSON]->(l)
                        MERGE (a)-[:LEARNED]->(l)
                        MERGE (a)-[:{rel}]->(t)
                    """, tid=task_id, status="done" if succeeded else "failed", p=planner, 
                         lid=str(uuid4()), text=lesson_text, tenant_id=tenant_id, cats=[task_type], rel=rel)
        await asyncio.to_thread(_neo4j_learn)
    except Exception as e:
        log.warning("neo4j_lesson_store_failed error=%s", e)

    # 4. PostgreSQL Audit
    try:
        pool = get_async_pool()


        async with pool.connection() as conn:

            import time
            latency = (time.time() - state["start_time"]) * 1000
            await conn.execute("""
                INSERT INTO cognitive_executions 
                (task_id, tenant_id, task_category, resolved_at_layer, success, latency_ms, tokens_used, tokens_saved)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, (task_id, tenant_id, task_type, state.get("cache_level", "llm"), succeeded, latency, state.get("tokens_used", 0), state.get("tokens_saved", 0)))

    except Exception as e:
        log.warning("db_audit_sync_failed error=%s", e)

    # 5. Sprint 4: Auto-promotion to L0 Speed Cache
    if succeeded and r:
        try:
            pattern_hash = hashlib.md5(description.strip().lower()[:100].encode()).hexdigest()
            count_key = f"sinc:pattern_freq:{tenant_id}:{pattern_hash}"
            count = await r.incr(count_key)
            await r.expire(count_key, 86400 * 7)
            if count >= 3 and confidence > 0.90:
                cache_key = f"l0_hot:{tenant_id}:{pattern_hash}"
                payload = json.dumps({"solution": solution, "lesson": lesson_text, "promoted_at": datetime.now().isoformat(), "hits": count})
                await r.setex(cache_key, 86400 * 3, payload)
        except Exception as e:
            log.warning("cache_promotion_failed error=%s", e)

    # 6. Phase 13: Evolutionary Distillation Trigger
    if verified:
        try:
            scheduled = await _maybe_schedule_distillation(
                tenant_id,
                verified=verified,
                redis_client=r,
            )
            if scheduled:
                log.info("distillation_threshold_reached tenant=%s", tenant_id)
        except Exception as e:
            log.debug("distillation_trigger_failed error=%s", e)

    return lesson_text
