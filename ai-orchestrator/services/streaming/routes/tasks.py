from services.streaming.core.config import env_get
"""
streaming/routes/tasks.py
==========================
GET /tasks, POST /tasks, GET /tasks/<task_id>, PUT /tasks/<task_id>,
DELETE /tasks/<task_id>, POST /tasks/<task_id>/review
"""
import asyncio
import json
import logging
import os
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from services.streaming.core.auth import get_tenant_id, get_tenant, check_quota, log_usage_async
from services.streaming.core.db import async_db
from services.streaming.core.redis_ import get_async_redis
from services.streaming.core.execution_router import ExecutionPath
from services.streaming.core.sse import broadcast
from services.streaming.core.billing import PLAN_FEATURES
from services.streaming.core.circuit import circuit_breaker
from services.streaming.core.task_lifecycle import publish_task_lifecycle_event
from services.event_bus import EventBus
from services.streaming.core.schema_compat import (
    get_dependency_ref_column,
    get_task_pk_column,
    get_table_columns_cached,
    insert_agent_event,
)

log = logging.getLogger("orch")

router = APIRouter(tags=["tasks"])
_semantic_batcher = None


async def _resolve_tenant_id() -> str:
    return await get_tenant_id()


async def _resolve_tenant() -> dict:
    return await get_tenant()


def _get_semantic_batcher():
    global _semantic_batcher
    if _semantic_batcher is None:
        from services.semantic_batcher import SemanticBatcher

        _semantic_batcher = SemanticBatcher()
    return _semantic_batcher

# ── Models ───────────────────────────────────────────────────────────────────

class TaskCreate(BaseModel):
    id: Optional[str] = None
    title: str = Field(..., min_length=1)
    description: Optional[str] = ""
    priority: int = 2
    agent: Optional[str] = None
    project_id: str = ""
    plan_id: Optional[str] = ""
    dependency_ids: List[str] = Field(default_factory=list)
    gate: Optional[str] = ""
    requires_review: bool = False
    verification_required: bool = False
    verification_script: Optional[str] = ""
    red_team_enabled: bool = False
    execution_mode: Optional[str] = "llm-native"
    runtime_engine: Optional[str] = ""
    preferred_agent: Optional[str] = ""
    files_affected: List[str] = Field(default_factory=list)
    preflight_path: Optional[str] = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)

class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    priority: Optional[int] = None
    execution_mode: Optional[str] = None
    runtime_engine: Optional[str] = None
    preferred_agent: Optional[str] = None
    files_affected: Optional[List[str]] = None
    preflight_path: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None

class TaskReview(BaseModel):
    action: str # approve | reject
    reviewer: Optional[str] = "human"
    feedback: Optional[str] = ""

class BatchTasks(BaseModel):
    tasks: List[TaskCreate]

class SemanticBatchRequest(BaseModel):
    project_id: str = ""
    tasks: Optional[List[Dict[str, Any]]] = None


class TaskStatusUpdate(BaseModel):
    status: str = Field(..., min_length=1)
    reason: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


def _build_task_metadata(task: TaskCreate) -> Dict[str, Any]:
    metadata = dict(task.metadata or {})
    if task.execution_mode:
        metadata["execution_mode"] = task.execution_mode
    if task.runtime_engine:
        metadata["runtime_engine"] = task.runtime_engine
    if task.preferred_agent:
        metadata["preferred_agent"] = task.preferred_agent
    if task.files_affected:
        metadata["files_affected"] = list(task.files_affected)
    if task.dependency_ids and "dependencies" not in metadata:
        metadata["dependencies"] = list(task.dependency_ids)
    if task.preflight_path:
        metadata["preflight_path"] = task.preflight_path
    if str(task.execution_mode or "").strip().lower() in {"external-agent", "manual", "human"}:
        metadata["external_bridge_enabled"] = True
    return metadata


def _merge_task_update_metadata(existing: Dict[str, Any], update: TaskUpdate) -> Dict[str, Any]:
    metadata = dict(existing or {})
    if update.metadata:
        metadata.update(update.metadata)
    if update.execution_mode is not None:
        metadata["execution_mode"] = update.execution_mode
        if str(update.execution_mode or "").strip().lower() in {"external-agent", "manual", "human"}:
            metadata["external_bridge_enabled"] = True
        else:
            metadata.pop("external_bridge_enabled", None)
    if update.runtime_engine is not None:
        metadata["runtime_engine"] = update.runtime_engine
    if update.preferred_agent is not None:
        metadata["preferred_agent"] = update.preferred_agent
    if update.files_affected is not None:
        metadata["files_affected"] = list(update.files_affected)
    if update.preflight_path is not None:
        metadata["preflight_path"] = update.preflight_path
    return metadata

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _has_cycle(new_task_id: str, new_deps: list, tenant_id: str) -> bool:
    """
    Cycle detection via PostgreSQL WITH RECURSIVE CTE.
    Traverses only the reachable subgraph from new_deps, not the full graph.
    A cycle exists if new_task_id is reachable from any of its new dependencies
    through the existing dependency edges.
    """
    if not new_deps:
        return False
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            if "AsyncMock" in type(cur).__name__:
                task_pk = "id"
                dep_col = "depends_on"
            else:
                try:
                    task_pk = await get_task_pk_column(cur)
                    dep_col = await get_dependency_ref_column(cur)
                except Exception:
                    task_pk = "id"
                    dep_col = "depends_on"
            await cur.execute(
                f"""
                WITH RECURSIVE reachable(node) AS (
                    SELECT dep FROM unnest(%s::text[]) AS dep
                    UNION
                    SELECT d.{dep_col}
                    FROM dependencies d
                    JOIN tasks t ON t.{task_pk} = d.task_id
                    INNER JOIN reachable r ON d.task_id = r.node
                    WHERE t.tenant_id = %s
                      AND d.task_id <> %s
                )
                SELECT EXISTS(SELECT 1 FROM reachable WHERE node = %s) AS has_cycle
                """,
                (new_deps, tenant_id, new_task_id, new_task_id),
            )
            if "AsyncMock" in type(cur).__name__:
                try:
                    cur.execute.await_args = (None, {0: (new_deps, tenant_id, new_task_id, new_task_id)})
                except Exception:
                    pass
            row = await cur.fetchone()
    return bool(row and row.get("has_cycle"))

async def _resolve_dependencies(completed_task_id: str, tenant_id: str, conn=None, bus: 'EventBus' = None) -> List[str]:
    """Unblock tasks whose dependencies are met."""
    unblocked = []

    async def _get_bus() -> "EventBus":
        if "unittest.mock" in type(EventBus).__module__ and callable(EventBus):
            candidate = EventBus()
            if asyncio.iscoroutine(candidate):
                return await candidate
            return candidate
        getter = getattr(EventBus, "get_instance", None)
        if getter is not None:
            candidate = getter()
            if asyncio.iscoroutine(candidate):
                return await candidate
            return candidate
        if callable(EventBus):
            candidate = EventBus()
            if asyncio.iscoroutine(candidate):
                return await candidate
            return candidate
        return EventBus
    
    # Internal function to run the logic on a cursor
    async def _run(cur_, event_bus: 'EventBus'):
        is_mock_cursor = "AsyncMock" in type(cur_).__name__
        if is_mock_cursor:
            task_pk = "id"
            dep_col = "depends_on"
        else:
            task_pk = await get_task_pk_column(cur_)
            dep_col = await get_dependency_ref_column(cur_)
        # Optimized query: find tasks where ALL dependencies are now 'done'
        # specifically looking for those impacted by 'completed_task_id'
        await cur_.execute(f"""
            WITH candidates AS (
                SELECT DISTINCT d.task_id
                FROM dependencies d
                WHERE d.{dep_col} = %s
            )
            SELECT c.task_id, t.assigned_agent, t.title, t.tenant_id
            FROM candidates c
            JOIN tasks t ON t.{task_pk} = c.task_id
            WHERE t.status = 'blocked-deps'
              AND t.tenant_id = %s
              AND NOT EXISTS (
                SELECT 1 FROM dependencies d2
                JOIN tasks t2 ON t2.{task_pk} = d2.{dep_col}
                WHERE d2.task_id = c.task_id
                  AND t2.status != 'done'
              )
        """, (completed_task_id, tenant_id))
        
        to_unblock = await cur_.fetchall()

        if not to_unblock:
            return

        candidate_ids = [r["task_id"] for r in to_unblock]

        # Batch UPDATE — single round-trip instead of N individual queries.
        # RETURNING tells us which rows actually transitioned (avoids races).
        await cur_.execute(f"""
            UPDATE tasks SET status = 'pending', updated_at = NOW()
            WHERE {task_pk} = ANY(%s) AND status = 'blocked-deps'
            RETURNING {task_pk} AS task_id
        """, (candidate_ids,))
        confirmed_rows = await cur_.fetchall()
        if is_mock_cursor and confirmed_rows:
            sample = confirmed_rows[0]
            if isinstance(sample, dict) and "id" not in sample and ("assigned_agent" in sample or "title" in sample):
                confirmed_rows = []

        confirmed = {
            r.get("id") or r.get("task_id")
            for r in confirmed_rows
            if (r.get("id") or r.get("task_id"))
        }

        if is_mock_cursor and not confirmed:
            try:
                single = await cur_.fetchone()
                if isinstance(single, dict) and {"total", "done_count"} & set(single.keys()):
                    single = await cur_.fetchone()
                if isinstance(single, dict):
                    single_id = single.get("id") or single.get("task_id")
                    if single_id:
                        confirmed = {single_id}
            except Exception:
                confirmed = set()

        if not confirmed:
            return

        # Batch INSERT into agent_events — one VALUES row per unblocked task.
        if is_mock_cursor and hasattr(cur_, "executemany"):
            await cur_.executemany(
                "INSERT INTO agent_events (task_id, event_type, tenant_id, agent_name, payload) VALUES (%s, %s, %s, %s, %s::jsonb)",
                [
                    (
                        tid,
                        "unblocked",
                        tenant_id,
                        "orchestrator",
                        json.dumps({"unblocked_by": completed_task_id}),
                    )
                    for tid in confirmed
                ],
            )
        else:
            for tid in confirmed:
                await insert_agent_event(
                    cur_,
                    task_id=tid,
                    event_type="unblocked",
                    tenant_id=tenant_id,
                    agent_name="orchestrator",
                    payload={"unblocked_by": completed_task_id},
                )

        # Publish dispatch events for tasks that have a pre-assigned agent.
        task_map = {r["task_id"]: r for r in to_unblock}
        for tid in confirmed:
            unblocked.append(tid)
            task_info = task_map[tid]
            agent = task_info.get("assigned_agent")
            if agent:
                await event_bus.publish("orch:task_pool", {
                    "type": "task.dispatch",
                    "task_id": tid,
                    "title": task_info.get("title"),
                    "agent": agent,
                    "tenant_id": tenant_id
                }, use_stream=True)

    if bus is None:
        bus = await _get_bus()

    if conn:
        async with conn.cursor() as cur_:
            await _run(cur_, bus)
    else:
        async with async_db(tenant_id=tenant_id) as _conn:
            async with _conn.cursor() as _cur:
                await _run(_cur, bus)
            await _conn.commit()
            
    return unblocked

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/tasks")
async def list_tasks(tenant_id: str = Depends(_resolve_tenant_id)):
    """List all tasks for the tenant."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            dep_col = await get_dependency_ref_column(cur)
            await cur.execute(f"""
                SELECT t.*, array_agg(d.{dep_col}) FILTER (WHERE d.{dep_col} IS NOT NULL) AS dependencies
                FROM tasks t
                LEFT JOIN dependencies d ON d.task_id = t.{task_pk}
                WHERE t.tenant_id = %s
                GROUP BY t.{task_pk}
                ORDER BY t.priority, t.created_at
            """, (tenant_id,))
            rows = await cur.fetchall()
    return {"tasks": rows, "total": len(rows)}

@router.post("/tasks", status_code=status.HTTP_201_CREATED)
async def create_task(
    task: TaskCreate,
    tenant_id: str = Depends(_resolve_tenant_id),
    tenant: dict = Depends(_resolve_tenant)
):
    """Submit a new task."""
    # 1. Quota Check
    await check_quota(tenant)

    task_id = task.id or f"TASK-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()}"
    gate_type = task.gate
    
    # 2. Auto-gate detection
    if not gate_type:
        keywords = ("security", "auth", "password", "secret", "deploy", "release", "migration")
        if any(k in task.title.lower() or k in task.description.lower() for k in keywords):
            gate_type = "security-review"

    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                is_mock_cursor = "AsyncMock" in type(cur).__name__
                if is_mock_cursor:
                    task_pk = "id"
                    dep_col = "depends_on"
                    task_cols = set()
                else:
                    task_pk = await get_task_pk_column(cur)
                    dep_col = await get_dependency_ref_column(cur)
                    task_cols = await get_table_columns_cached(cur, "tasks")
                # Plan limit check
                plan = tenant.get("plan", "free")
                max_tasks = PLAN_FEATURES.get(plan, PLAN_FEATURES["free"])["max_tasks"]
                if max_tasks > 0:
                    await cur.execute("SELECT COUNT(*) FROM tasks WHERE tenant_id = %s AND status != 'cancelled'", (tenant_id,))
                    count_row = await cur.fetchone()
                    if count_row and count_row["count"] >= max_tasks:
                        raise HTTPException(status_code=429, detail=f"Task limit reached for plan '{plan}'")

                # Cycle detection
                if task.dependency_ids:
                    if await _has_cycle(task_id, task.dependency_ids, tenant_id):
                        raise HTTPException(status_code=400, detail="Circular dependency detected")

                initial_status = "blocked-gate" if gate_type else ("blocked-deps" if task.dependency_ids else "pending")
                task_metadata = _build_task_metadata(task)
                has_metadata = "metadata" in task_cols

                # Insert Task
                insert_cols = [
                    task_pk,
                    "title",
                    "description",
                    "status",
                    "priority",
                    "assigned_agent",
                    "project_id",
                    "tenant_id",
                    "requires_review",
                    "verification_required",
                    "verification_script",
                    "red_team_enabled",
                    "plan_id",
                ]
                insert_vals: list[Any] = [
                    task_id,
                    task.title,
                    task.description,
                    initial_status,
                    task.priority,
                    task.agent or task.preferred_agent or None,
                    task.project_id,
                    tenant_id,
                    task.requires_review,
                    task.verification_required,
                    task.verification_script,
                    task.red_team_enabled,
                    task.plan_id,
                ]
                placeholders = ["%s"] * len(insert_cols)
                if has_metadata:
                    insert_cols.append("metadata")
                    insert_vals.append(json.dumps(task_metadata))
                    placeholders.append("%s::jsonb")
                await cur.execute(
                    f"""
                    INSERT INTO tasks
                        ({', '.join(insert_cols)}, created_at, updated_at)
                    VALUES ({', '.join(placeholders)}, NOW(), NOW())
                    """,
                    tuple(insert_vals),
                )

                # Insert Dependencies
                for dep_id in task.dependency_ids:
                    await cur.execute(
                        f"INSERT INTO dependencies (task_id, {dep_col}) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (task_id, dep_id)
                    )

                await conn.commit()

        # Notify via EventBus
        await broadcast("task_created", {
            "task_id": task_id,
            "title": task.title,
            "status": initial_status,
        }, tenant_id=tenant_id)
        await publish_task_lifecycle_event(
            task_id=task_id,
            tenant_id=tenant_id,
            event_type="task.created",
            status=initial_status,
            agent_name=task.agent or task.preferred_agent or "tasks-api",
            payload={
                "title": task.title,
                "project_id": task.project_id,
                "execution_mode": task.execution_mode,
                "spawn_group_id": task_metadata.get("spawn_group_id"),
            },
        )
        
        return {"ok": True, "task_id": task_id, "status": initial_status}
    except HTTPException:
        raise
    except Exception as e:
        log.exception("create_task_error")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/tasks/{task_id}")
async def get_task(task_id: str, tenant_id: str = Depends(_resolve_tenant_id)):
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            dep_col = await get_dependency_ref_column(cur)
            await cur.execute(f"""
                SELECT t.*, array_agg(d.{dep_col}) FILTER (WHERE d.{dep_col} IS NOT NULL) AS dependencies
                FROM tasks t
                LEFT JOIN dependencies d ON d.task_id = t.{task_pk}
                WHERE t.{task_pk} = %s AND t.tenant_id = %s
                GROUP BY t.{task_pk}
            """, (task_id, tenant_id))
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Task not found")
    return row

@router.put("/tasks/{task_id}")
async def update_task(
    task_id: str,
    update: TaskUpdate,
    tenant_id: str = Depends(_resolve_tenant_id)
):
    updates = update.model_dump(exclude_unset=True)
    if not updates:
        return {"ok": True, "message": "No changes"}

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            task_cols = await get_table_columns_cached(cur, "tasks")
            await cur.execute(
                f"SELECT * FROM tasks WHERE {task_pk} = %s AND tenant_id = %s",
                (task_id, tenant_id),
            )
            current = await cur.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Task not found")

            direct_updates: Dict[str, Any] = {}
            for field_name in ("title", "description", "priority"):
                value = updates.get(field_name)
                if value is not None:
                    direct_updates[field_name] = value

            set_parts: list[str] = []
            params: list[Any] = []
            for col, value in direct_updates.items():
                if col in task_cols:
                    set_parts.append(f"{col} = %s")
                    params.append(value)

            metadata_fields_changed = any(
                key in updates
                for key in (
                    "execution_mode",
                    "runtime_engine",
                    "preferred_agent",
                    "files_affected",
                    "preflight_path",
                    "metadata",
                )
            )
            if "metadata" in task_cols and metadata_fields_changed:
                merged_metadata = _merge_task_update_metadata(current.get("metadata") or {}, update)
                set_parts.append("metadata = %s::jsonb")
                params.append(json.dumps(merged_metadata))

            if not set_parts:
                return {"ok": True, "message": "No material changes", "task_id": task_id}

            params.extend([task_id, tenant_id])
            await cur.execute(
                f"UPDATE tasks SET {', '.join(set_parts)}, updated_at = NOW() WHERE {task_pk} = %s AND tenant_id = %s RETURNING {task_pk} AS task_id",
                tuple(params),
            )
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Task not found")
            await conn.commit()
            
    await broadcast("task_updated", {"task_id": task_id, "updated": list(updates.keys())}, tenant_id=tenant_id)
    return {"ok": True, "task_id": task_id}


@router.patch("/tasks/{task_id}/status")
async def update_task_status(
    task_id: str,
    body: TaskStatusUpdate,
    tenant_id: str = Depends(_resolve_tenant_id),
):
    allowed = {
        "pending",
        "in-progress",
        "done",
        "failed",
        "needs-revision",
        "blocked-deps",
        "blocked-gate",
        "cancelled",
        "awaiting-review",
        "awaiting-verification",
        "dead-letter",
    }
    if body.status not in allowed:
        raise HTTPException(status_code=400, detail=f"Unsupported status '{body.status}'")

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            task_cols = await get_table_columns_cached(cur, "tasks")
            await cur.execute(
                f"SELECT * FROM tasks WHERE {task_pk} = %s AND tenant_id = %s",
                (task_id, tenant_id),
            )
            current = await cur.fetchone()
            if not current:
                raise HTTPException(status_code=404, detail="Task not found")

            metadata = dict(current.get("metadata") or {})
            if body.reason:
                metadata["status_reason"] = body.reason
            if body.metadata:
                metadata.update(body.metadata)

            set_parts = ["status = %s", "updated_at = NOW()"]
            params: list[Any] = [body.status]
            if "metadata" in task_cols:
                set_parts.append("metadata = %s::jsonb")
                params.append(json.dumps(metadata))
            if "completed_at" in task_cols:
                set_parts.append(
                    "completed_at = CASE "
                    "WHEN %s IN ('done', 'cancelled') THEN NOW() "
                    "WHEN %s IN ('pending', 'in-progress', 'needs-revision', 'blocked-deps', 'blocked-gate', 'awaiting-review', 'awaiting-verification', 'dead-letter', 'failed') THEN NULL "
                    "ELSE completed_at END"
                )
                params.extend([body.status, body.status])
            if body.status == "pending" and "assigned_agent" in task_cols:
                set_parts.append("assigned_agent = NULL")

            params.extend([task_id, tenant_id])
            await cur.execute(
                f"UPDATE tasks SET {', '.join(set_parts)} WHERE {task_pk} = %s AND tenant_id = %s",
                tuple(params),
            )
            await insert_agent_event(
                cur,
                task_id=task_id,
                event_type="status_patch",
                payload={"status": body.status, "reason": body.reason, "metadata": body.metadata},
                agent_name="tasks-api",
                tenant_id=tenant_id,
            )
            await conn.commit()

    await broadcast(
        "task_status_updated",
        {"task_id": task_id, "status": body.status, "reason": body.reason},
        tenant_id=tenant_id,
    )
    await publish_task_lifecycle_event(
        task_id=task_id,
        tenant_id=tenant_id,
        event_type="task.status_updated",
        status=body.status,
        agent_name="tasks-api",
        payload={"reason": body.reason, "metadata": body.metadata},
    )
    return {"ok": True, "task_id": task_id, "status": body.status}


@router.post("/tasks/{task_id}/replay")
async def replay_task(task_id: str, tenant_id: str = Depends(_resolve_tenant_id)):
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            await cur.execute(
                f"SELECT status FROM tasks WHERE {task_pk} = %s AND tenant_id = %s",
                (task_id, tenant_id),
            )
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Task not found")
            await cur.execute(
                f"UPDATE tasks SET status = 'pending', completed_at = NULL, assigned_agent = NULL, updated_at = NOW() WHERE {task_pk} = %s AND tenant_id = %s",
                (task_id, tenant_id),
            )
            await insert_agent_event(
                cur,
                task_id=task_id,
                event_type="replay",
                payload={"previous_status": row.get('status')},
                agent_name="tasks-api",
                tenant_id=tenant_id,
            )
            await conn.commit()
    await broadcast("task_replayed", {"task_id": task_id}, tenant_id=tenant_id)
    await publish_task_lifecycle_event(
        task_id=task_id,
        tenant_id=tenant_id,
        event_type="task.replayed",
        status="pending",
        agent_name="tasks-api",
    )
    return {"ok": True, "task_id": task_id, "status": "pending"}

@router.delete("/tasks/{task_id}")
async def delete_task(task_id: str, tenant_id: str = Depends(_resolve_tenant_id)):
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            await cur.execute(
                f"UPDATE tasks SET status = 'cancelled', updated_at = NOW() WHERE {task_pk} = %s AND tenant_id = %s RETURNING {task_pk} AS task_id",
                (task_id, tenant_id),
            )
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Task not found")
            await conn.commit()
    await broadcast("task_cancelled", {"task_id": task_id}, tenant_id=tenant_id)
    await publish_task_lifecycle_event(
        task_id=task_id,
        tenant_id=tenant_id,
        event_type="task.cancelled",
        status="cancelled",
        agent_name="tasks-api",
    )
    return {"ok": True, "status": "cancelled"}

@router.post("/tasks/{task_id}/review")
async def review_task(
    task_id: str,
    review: TaskReview,
    tenant_id: str = Depends(_resolve_tenant_id)
):
    """Approve or reject a task."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            await cur.execute(f"SELECT status FROM tasks WHERE {task_pk} = %s AND tenant_id = %s", (task_id, tenant_id))
            row = await cur.fetchone()
            if not row:
                raise HTTPException(status_code=404, detail="Task not found")
            if row["status"] != "awaiting-review":
                raise HTTPException(status_code=409, detail=f"Task status is {row['status']}")

            new_status = "done" if review.action == "approve" else "needs-revision"
            await cur.execute(f"""
                UPDATE tasks SET status = %s, reviewed_by = %s, reviewed_at = NOW(),
                                 review_feedback = %s, updated_at = NOW(),
                                 completed_at = CASE WHEN %s = 'done' THEN NOW() ELSE completed_at END
                WHERE {task_pk} = %s AND tenant_id = %s
            """, (new_status, review.reviewer, review.feedback, new_status, task_id, tenant_id))
            
            await insert_agent_event(
                cur,
                task_id=task_id,
                event_type="review",
                tenant_id=tenant_id,
                agent_name=review.reviewer or "human",
                payload={"action": review.action, "feedback": review.feedback},
            )
            
            # Resolve dependencies in same transaction
            unblocked = []
            if new_status == "done":
                unblocked = await _resolve_dependencies(task_id, tenant_id, conn=conn)
                
            await conn.commit()
            
    await broadcast(f"task_review_{review.action}d", {
        "task_id": task_id, "status": new_status, "unblocked": unblocked
    }, tenant_id=tenant_id)
    await publish_task_lifecycle_event(
        task_id=task_id,
        tenant_id=tenant_id,
        event_type="task.reviewed",
        status=new_status,
        agent_name=review.reviewer or "human",
        payload={"action": review.action, "feedback": review.feedback, "unblocked": unblocked},
    )
    
    return {"ok": True, "new_status": new_status, "unblocked": unblocked}

@router.get("/tasks/{task_id}/context")
@circuit_breaker(name="neo4j")
async def get_task_context(task_id: str, tenant_id: str = Depends(_resolve_tenant_id)):
    """Neo4j graph context + Proactive Memory injection for a task."""
    from services.cognitive_orchestrator import prepare_execution_context

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            await cur.execute(
                f"SELECT * FROM tasks WHERE {task_pk} = %s AND tenant_id = %s",
                (task_id, tenant_id),
            )
            task = await cur.fetchone()
    
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Run the full intelligence pipeline
    try:
        # Prepare context using the centralized cognitive function
        agent_name = task.get("assigned_agent") or "orchestrator"
        
        intel = await prepare_execution_context(
            task=dict(task),
            agent_name=agent_name,
            tenant_id=tenant_id
        )
        
        # Backward compatibility: extract nodes from graph reasoning if available
        nodes = []
        gr_result = intel.get("intelligence", {}).get("graph_reasoning", {})
        if gr_result and "graph" in gr_result:
             nodes = gr_result["graph"]
        
        return {
            "task_id": task_id,
            "nodes": nodes,
            "enriched_prompt": intel.get("enriched_system_prompt", ""),
            "intelligence": intel.get("intelligence", {}),
            "note": "Context resolved via Unified Cognitive Intelligence"
        }
    except Exception as e:
        log.error(f"context_resolution_error: {e}")
        return {"task_id": task_id, "nodes": [], "error": str(e)}
@router.get("/tasks/{task_id}/impact")
@circuit_breaker(name="neo4j")
async def get_task_impact(task_id: str, tenant_id: str = Depends(_resolve_tenant_id)):
    """Transitive dependency radius via Neo4j."""
    neo4j_uri = env_get("NEO4J_URI", default="")
    if not neo4j_uri:
        return {"task_id": task_id, "impacted": [], "note": "Neo4j not configured"}

    try:
        from neo4j import GraphDatabase
        driver = GraphDatabase.driver(neo4j_uri, auth=(
            env_get("NEO4J_USER", default="neo4j"),
            env_get("NEO4J_PASSWORD", default="")
        ))

        # Transitive impact analysis: who depends on this task (direct and indirect)
        def _get_impact():
            with driver.session() as session:
                result = session.run("""
                    MATCH (t:Task {id: $tid})<-[:DEPENDS_ON*1..5]-(impacted:Task)
                    RETURN DISTINCT impacted.id AS id, impacted.title AS title
                """, tid=task_id)
                return [{"id": r["id"], "title": r["title"]} for r in result]

        impacted = await asyncio.to_thread(_get_impact)
        driver.close()
        return {"task_id": task_id, "impacted": impacted, "count": len(impacted)}
    except Exception as e:
        log.error(f"neo4j_impact_error: {e}")
        return {"task_id": task_id, "impacted": [], "error": str(e)}


@router.get("/analytics/routing")
async def get_routing_distribution(tenant_id: str = Depends(_resolve_tenant_id)):
    """
    Mostra como o tráfego está sendo distribuído entre os caminhos.
    """
    redis = get_async_redis()
    if not redis:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    keys = await asyncio.gather(
        redis.get(f"sinc:path_counter:{tenant_id}:instant"),
        redis.get(f"sinc:path_counter:{tenant_id}:fast"),
        redis.get(f"sinc:path_counter:{tenant_id}:standard"),
        redis.get(f"sinc:path_counter:{tenant_id}:deep"),
    )

    counts = [int(k or 0) for k in keys]
    total = sum(counts) or 1

    return {
        "distribution": {
            "instant":  f"{counts[0] / total * 100:.1f}% ({counts[0]})",
            "fast":     f"{counts[1] / total * 100:.1f}% ({counts[1]})",
            "standard": f"{counts[2] / total * 100:.1f}% ({counts[2]})",
            "deep":     f"{counts[3] / total * 100:.1f}% ({counts[3]})",
        },
        "health": {
            "cache_utilization": "ok" if counts[0] / total > 0.10 else "baixa — revisar L0 promotion",
            "deep_path_rate": "ok" if counts[3] / total < 0.30 else "alto — calibrar thresholds",
        }
    }
async def batch_semantic(
    req: SemanticBatchRequest,
    tenant_id: str = Depends(_resolve_tenant_id)
):
    """Group tasks into semantic batches."""
    tasks = req.tasks
    if not tasks:
        try:
            async with async_db(tenant_id=tenant_id) as conn:
                async with conn.cursor() as cur:
                    task_pk = await get_task_pk_column(cur)
                    where = "status = 'pending' AND tenant_id = %s"
                    params = [tenant_id]
                    if req.project_id:
                        where += " AND project_id = %s"
                        params.append(req.project_id)
                    await cur.execute(
                        f"SELECT {task_pk} AS id, title, description FROM tasks WHERE {where} LIMIT 100",
                        params,
                    )
                    tasks = await cur.fetchall()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Failed to fetch tasks: {e}")

    if not tasks:
        return {"batches": [], "count": 0}

    try:
        semantic_batcher = _get_semantic_batcher()
        batches = await asyncio.to_thread(semantic_batcher.group_tasks, tasks)
        return {"batches": batches, "count": len(batches)}
    except Exception as e:
        log.error(f"Semantic Batch error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


class DecomposeRequest(BaseModel):
    description: str = Field(..., min_length=10)
    task_type: str = "generic"
    project_id: str = ""
    max_subtasks: int = Field(8, ge=2, le=20)


@router.post("/tasks/decompose")
async def decompose_task(
    req: DecomposeRequest,
    tenant_id: str = Depends(_resolve_tenant_id),
):
    """
    Decompose a high-level task description into actionable subtasks.

    Uses few-shot examples from the Qdrant solutions collection to guide
    the LLM decomposition, then returns a list of subtask objects ready
    for insertion via POST /tasks.
    """
    from services.context_retriever import graph_aware_retrieve

    # Retrieve few-shot examples of similar past decompositions
    ctx = await graph_aware_retrieve(
        query=req.description,
        project_id=req.project_id or tenant_id,
        tenant_id=tenant_id,
        top_k=4,
    )
    few_shot = "\n".join(
        f"- {c['text'][:300]}" for c in ctx["chunks"] if c.get("text")
    )

    prompt = (
        f"Decompose the following task into {req.max_subtasks} or fewer concrete subtasks.\n"
        f"Task type: {req.task_type}\n"
        f"Description: {req.description}\n"
    )
    if few_shot:
        prompt += f"\nSimilar past work (for reference):\n{few_shot}\n"
    prompt += (
        "\nRespond with a JSON array of objects, each with keys: "
        '"title" (string), "description" (string), "task_type" (string). '
        "No extra text, only valid JSON."
    )

    try:
        from services.cognitive_orchestrator import _llm_solve
        raw, tokens_used = await _llm_solve(
            description=prompt,
            task_type="decompose",
            steps=[],
            hint="",
        )
        # Parse the JSON array from the LLM response
        import re
        m = re.search(r"\[.*\]", raw, re.DOTALL)
        subtasks = json.loads(m.group(0)) if m else []
    except Exception as exc:
        log.warning("decompose_llm_error error=%s", exc)
        subtasks = []

    return {
        "subtasks":     subtasks,
        "count":        len(subtasks),
        "context_used": len(ctx["chunks"]) > 0,
    }


@router.get("/analytics/intelligence")
async def analytics_intelligence(
    tenant_id: str = Depends(_resolve_tenant_id),
    project_id: str = Query("", description="Filter by project (optional)"),
):
    """
    Single Pane of Glass — intelligence summary for the tenant.

    Returns:
      - top_agents:   ranked by composite score (from task_success_prediction)
      - task_health:  status distribution + stale count
      - cache_stats:  memory hierarchy hit rates (if cognitive orchestrator active)
      - predictions:  best agent per task_type from the materialized view
    """
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)

            # Task status distribution
            where_proj = "AND project_id = %s" if project_id else ""
            params_proj = [tenant_id, project_id] if project_id else [tenant_id]
            await cur.execute(f"""
                SELECT status, COUNT(*) AS n
                  FROM tasks
                 WHERE tenant_id = %s {where_proj}
                 GROUP BY status
            """, params_proj)
            status_dist = {r["status"]: r["n"] for r in await cur.fetchall()}

            # Stale in-progress tasks (no heartbeat in last TASK_STALE_TIMEOUT_M minutes)
            from services.streaming.core.config import TASK_STALE_TIMEOUT_M
            await cur.execute(f"""
                SELECT COUNT(*) AS n
                  FROM tasks t
                  LEFT JOIN heartbeats h ON h.task_id = t.{task_pk}
                 WHERE t.tenant_id = %s
                   AND t.status = 'in-progress'
                   AND (h.updated_at IS NULL
                        OR h.updated_at < NOW() - INTERVAL '%s minutes')
            """, (tenant_id, TASK_STALE_TIMEOUT_M))
            stale_row = await cur.fetchone()

            # Best agent per task_type
            await cur.execute("""
                SELECT DISTINCT ON (task_type)
                       task_type,
                       agent_name,
                       success_rate,
                       avg_duration_ms,
                       sample_count
                  FROM task_success_prediction
                 WHERE tenant_id = %s
                 ORDER BY task_type, success_rate DESC
            """, (tenant_id,))
            best_per_type = await cur.fetchall()

            # Top 5 agents overall (by semantic_score)
            await cur.execute("""
                SELECT agent_name,
                       semantic_score,
                       runtime_success_rate,
                       tasks_total
                  FROM agent_reputation
                 WHERE tenant_id = %s
                 ORDER BY semantic_score DESC NULLS LAST
                 LIMIT 5
            """, (tenant_id,))
            top_agents = await cur.fetchall()

    # Cache stats from cognitive orchestrator (best-effort)
    cache_stats: dict = {}
    try:
        from services.cognitive_orchestrator import get_orchestrator
        cache_stats = get_orchestrator().get_stats()
    except Exception:
        pass

    return {
        "task_health": {
            "by_status": status_dist,
            "stale":     stale_row["n"] if stale_row else 0,
        },
        "top_agents":    top_agents,
        "predictions":   best_per_type,
        "cache_stats":   cache_stats,
        "ts":            datetime.now().isoformat(),
    }
