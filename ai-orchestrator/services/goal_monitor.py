"""
goal_monitor.py
===============
Background monitoring and adaptation for Living Goals (Nível 5).
Detects prerequisites, discovered work, and stagnation during goal execution.
"""
import asyncio
import json
import logging
from typing import List, Any
from uuid import uuid4

log = logging.getLogger("orchestrator.goal.monitor")

async def monitor_and_adapt_goal(goal_id: str, tenant_id: str):
    """
    Background loop that runs while the goal is active.
    Aligned with prompt Módulo 3.1.
    """
    log.info(f"GOAL_MONITOR_START goal={goal_id}")
    
    from services.streaming.core.db import get_pool
    pool = await get_pool()
    
    while True:
        try:
            async with pool.acquire() as conn:
                # 1. Check goal status
                goal = await conn.fetchrow("SELECT status FROM goals WHERE id = $1", goal_id)
                if not goal or goal["status"] not in ["executing", "validating"]:
                    break
                
                # 2. Get all tasks for this goal
                tasks = await conn.fetch("SELECT * FROM tasks WHERE goal_id = $1", goal_id)
                
                # 3. Evaluate adaptations
                adaptations = await _evaluate_adaptations(goal_id, tasks, tenant_id)
                
                # 4. Apply adaptations
                for adaptation in adaptations:
                    await _apply_adaptation(conn, goal_id, tenant_id, adaptation)
            
            await asyncio.sleep(30) # Poll every 30s as per prompt
        except Exception as e:
            log.error(f"goal_monitor_error: {e}")
            await asyncio.sleep(60)

async def _evaluate_adaptations(goal_id: str, tasks: List[Any], tenant_id: str) -> List[dict]:
    adaptations = []
    
    done = [t for t in tasks if t["status"] == "done"]
    failed = [t for t in tasks if t["status"] in ["dead-letter", "failed"]]
    pending = [t for t in tasks if t["status"] == "pending"]
    
    # Adaptation: Add Subtasks from discovered work
    for task in done:
        payload = json.loads(task.get("metadata", "{}")).get("completion_payload", {})
        discovered_work = payload.get("discovered_work", [])
        for work in discovered_work:
            # Simple check to avoid duplicates
            if not any(work.lower() in t["description"].lower() for t in tasks):
                adaptations.append({
                    "type": "add_subtask",
                    "reason": f"Discovered by {task['id']}: {work}",
                    "work_description": work
                })

    # Adaptation: Reorder (Missing prerequisite)
    for task in failed:
        failure_hint = json.loads(task.get("metadata", "{}")).get("failure_hint")
        if failure_hint:
            # If hint suggests a missing requirement, find a pending task that matches or create one
            adaptations.append({
                "type": "reorder",
                "reason": f"Failure hint in {task['id']} suggests missing: {failure_hint}",
                "failed_task_id": task["id"],
                "prerequisite": failure_hint
            })

    # Adaptation: Escalate if failure rate is high
    if len(tasks) > 3 and (len(failed) / len(tasks)) > 0.4:
        adaptations.append({
            "type": "escalate",
            "reason": f"High failure rate: {len(failed)}/{len(tasks)}"
        })

    return adaptations

async def _apply_adaptation(conn: Any, goal_id: str, tenant_id: str, adapt: dict):
    log.info(f"APPLYING_ADAPTATION goal={goal_id} type={adapt['type']}")
    
    # Log adaptation
    adaptation_id = await conn.fetchval("""
        INSERT INTO goal_adaptations (goal_id, tenant_id, adaptation_type, reason, affected_tasks)
        VALUES ($1, $2, $3, $4, $5) RETURNING id
    """, goal_id, tenant_id, adapt["type"], adapt["reason"], json.dumps(adapt.get("failed_task_id", [])))

    if adapt["type"] == "add_subtask":
        # Create new task linked to this goal
        await conn.execute("""
            INSERT INTO tasks (tenant_id, goal_id, description, status, category)
            VALUES ($1, $2, $3, 'pending', 'discovered')
        """, tenant_id, goal_id, adapt["work_description"])

    elif adapt["type"] == "escalate":
        await conn.execute("UPDATE goals SET status = 'failed', metadata = metadata || '{\"escalation\": \"high_failure_rate\"}' WHERE id = $1", goal_id)
