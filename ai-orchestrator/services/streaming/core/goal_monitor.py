from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from services.streaming.core.db import async_db
from services.streaming.core.schema_compat import (
    get_dependency_ref_column,
    get_table_columns_cached,
    get_task_pk_column,
)
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator.goal_monitor")

_ACTIVE_GOAL_STATUSES = {"planning", "executing", "validating", "pending", "in-progress"}


@dataclass
class GoalAdaptation:
    adaptation_type: str  # reorder | cancel_subtask | add_subtask | escalate
    affected_tasks: list[str]
    reason: str
    applied: bool = False


async def monitor_and_adapt_goal(goal_id: str, tenant_id: str) -> None:
    """Periodically reconcile an executing goal and apply lightweight adaptations."""
    while True:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT status, project_id FROM goals WHERE id = %s", (goal_id,))
                goal = await cur.fetchone()
                if not goal or str(goal.get("status") or "").strip().lower() not in _ACTIVE_GOAL_STATUSES:
                    break

                task_cols = await get_table_columns_cached(cur, "tasks")
                task_pk = await get_task_pk_column(cur)
                completion_select = (
                    "completion_payload"
                    if "completion_payload" in task_cols
                    else "NULL::jsonb AS completion_payload"
                )
                failure_hint_select = (
                    "failure_hint" if "failure_hint" in task_cols else "NULL::text AS failure_hint"
                )
                await cur.execute(
                    f"""
                    SELECT {task_pk} AS id, status, title, {completion_select}, {failure_hint_select}
                    FROM tasks
                    WHERE goal_id = %s
                    """,
                    (goal_id,),
                )
                tasks = [dict(row) for row in await cur.fetchall()]

        adaptations = await evaluate_goal_adaptations(goal_id, tasks, tenant_id)
        for adaptation in adaptations:
            await apply_goal_adaptation(goal_id, adaptation, tenant_id)
            if adaptation.applied:
                log.info(
                    "goal_adapted goal=%s type=%s reason=%s",
                    goal_id,
                    adaptation.adaptation_type,
                    adaptation.reason,
                )
                await broadcast(
                    "goal_adaptation",
                    {
                        "goal_id": goal_id,
                        "type": adaptation.adaptation_type,
                        "reason": adaptation.reason,
                    },
                    tenant_id=tenant_id,
                )

        await asyncio.sleep(30)


async def evaluate_goal_adaptations(
    goal_id: str,
    tasks: list[dict[str, Any]],
    tenant_id: str,
) -> list[GoalAdaptation]:
    del goal_id, tenant_id
    adaptations: list[GoalAdaptation] = []

    failed = [t for t in tasks if t.get("status") in ("failed", "dead-letter")]
    done = [t for t in tasks if t.get("status") == "done"]
    pending = [t for t in tasks if t.get("status") in ("pending", "blocked-deps")]

    for failed_task in failed:
        hint = str(failed_task.get("failure_hint") or "").strip().lower()
        if not hint:
            continue
        for pending_task in pending:
            pending_title = str(pending_task.get("title") or "").strip().lower()
            if pending_title and (pending_title in hint or hint in pending_title):
                adaptations.append(
                    GoalAdaptation(
                        adaptation_type="reorder",
                        affected_tasks=[str(pending_task["id"]), str(failed_task["id"])],
                        reason=(
                            f"Tarefa '{failed_task.get('title', '')}' falhou por dependência "
                            f"ausente em '{pending_task.get('title', '')}'"
                        ),
                    )
                )

    for completed_task in done:
        payload = completed_task.get("completion_payload") or {}
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except Exception:
                payload = {}

        discovered = payload.get("discovered_work", []) if isinstance(payload, dict) else []
        for work in discovered:
            work_text = str(work or "").strip()
            if not work_text:
                continue
            if not any(work_text.lower() in str(task.get("title") or "").lower() for task in tasks):
                adaptations.append(
                    GoalAdaptation(
                        adaptation_type="add_subtask",
                        affected_tasks=[],
                        reason=f"Trabalho descoberto em '{completed_task.get('title', '')}': {work_text}",
                    )
                )

    if len(tasks) > 3:
        fail_rate = len(failed) / len(tasks)
        if fail_rate > 0.4:
            adaptations.append(
                GoalAdaptation(
                    adaptation_type="escalate",
                    affected_tasks=[str(task["id"]) for task in failed],
                    reason=f"Alta taxa de falha ({fail_rate:.0%}) no Goal",
                )
            )

    return adaptations


async def apply_goal_adaptation(goal_id: str, adaptation: GoalAdaptation, tenant_id: str) -> None:
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            dep_ref_col = await get_dependency_ref_column(cur)
            task_cols = await get_table_columns_cached(cur, "tasks")
            await cur.execute("SELECT project_id FROM goals WHERE id = %s", (goal_id,))
            goal = await cur.fetchone()
            project_id = (goal or {}).get("project_id", "")

            if adaptation.adaptation_type == "reorder":
                pending_id, failed_id = adaptation.affected_tasks
                await cur.execute(
                    f"""
                    INSERT INTO dependencies (task_id, {dep_ref_col})
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (failed_id, pending_id),
                )
                await cur.execute(
                    f"UPDATE tasks SET status = 'blocked-deps', updated_at = NOW() WHERE {task_pk} = %s",
                    (failed_id,),
                )
                adaptation.applied = True

            elif adaptation.adaptation_type == "add_subtask":
                task_id = f"TASK-ADAPT-{uuid4().hex[:12]}"
                insert_cols = [task_pk, "title", "description", "status", "tenant_id"]
                insert_vals: list[Any] = [
                    task_id,
                    (adaptation.reason[:120] or "Goal adaptation follow-up"),
                    adaptation.reason,
                    "pending",
                    tenant_id,
                ]
                placeholders = ["%s"] * len(insert_cols)
                if "project_id" in task_cols:
                    insert_cols.append("project_id")
                    insert_vals.append(project_id)
                    placeholders.append("%s")
                if "goal_id" in task_cols:
                    insert_cols.append("goal_id")
                    insert_vals.append(goal_id)
                    placeholders.append("%s")
                if "metadata" in task_cols:
                    insert_cols.append("metadata")
                    insert_vals.append(json.dumps({"goal_adaptation": True, "source": "goal_monitor"}))
                    placeholders.append("%s::jsonb")
                await cur.execute(
                    f"""
                    INSERT INTO tasks ({', '.join(insert_cols)}, created_at, updated_at)
                    VALUES ({', '.join(placeholders)}, NOW(), NOW())
                    """,
                    tuple(insert_vals),
                )
                adaptation.applied = True

            elif adaptation.adaptation_type == "escalate":
                await cur.execute(
                    "UPDATE goals SET status = 'awaiting-review', updated_at = NOW() WHERE id = %s",
                    (goal_id,),
                )
                adaptation.applied = True

            if adaptation.applied:
                await cur.execute(
                    """
                    INSERT INTO goal_adaptations (goal_id, tenant_id, adaptation_type, affected_tasks, reason)
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        goal_id,
                        tenant_id,
                        adaptation.adaptation_type,
                        json.dumps(adaptation.affected_tasks),
                        adaptation.reason,
                    ),
                )
                await conn.commit()
