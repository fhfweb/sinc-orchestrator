"""
Compatibility router for the former orchestrator-core service.

The canonical control plane now lives on services.streaming (port 8765), but a
few internal workers still speak the old core contract. This router absorbs the
minimum operational surface so the 8767 service can be removed from the
official compose without losing behavior.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from services.streaming.core.auth import get_tenant_id
from services.streaming.core.db import async_db
from services.streaming.core.runtime_plane import scheduler_tick_once
from services.streaming.core.schema_compat import (
    get_table_columns_cached,
    get_task_pk_column,
    insert_agent_event,
)
from services.streaming.routes.agents import (
    Completion,
    Heartbeat,
    _apply_agent_completion,
    _publish_completion_audit,
    _sync_digital_twin,
    agent_heartbeat_api,
)

router = APIRouter(tags=["core_compat"])


async def _table_has_tenant(cur, table_name: str) -> bool:
    return "tenant_id" in await get_table_columns_cached(cur, table_name)


def _normalize_task_row(row: dict[str, Any], task_pk: str) -> dict[str, Any]:
    normalized = dict(row or {})
    normalized["id"] = normalized.get(task_pk) or normalized.get("id")
    if task_pk != "id":
        normalized.setdefault(task_pk, normalized["id"])
    return normalized


def _normalize_completion_status(status: str) -> str:
    normalized = str(status or "").strip().lower()
    if normalized in {"done", "completed", "complete", "success"}:
        return "success"
    if normalized in {"partial", "partial-success", "partial_success"}:
        return "partial"
    if normalized in {"failed", "failure", "error"}:
        return "failed"
    return "success"


def _build_tag_filters(tags: list[str], task_cols: set[str]) -> tuple[str, list[Any]]:
    safe_tags = [str(tag or "").strip() for tag in tags if str(tag or "").strip()]
    if not safe_tags:
        return "", []

    clauses: list[str] = []
    params: list[Any] = []
    for tag in safe_tags:
        upper = tag.upper()
        lower_like = f"%{tag.lower()}%"
        if upper.startswith("P") and upper[1:].isdigit():
            clauses.append("CAST(COALESCE(priority, 999) AS TEXT) = %s")
            params.append(upper[1:])
            continue
        tag_clauses = []
        if "title" in task_cols:
            tag_clauses.append("LOWER(COALESCE(title, '')) LIKE %s")
            params.append(lower_like)
        if "description" in task_cols:
            tag_clauses.append("LOWER(COALESCE(description, '')) LIKE %s")
            params.append(lower_like)
        if "metadata" in task_cols:
            tag_clauses.append("LOWER(COALESCE(metadata::text, '')) LIKE %s")
            params.append(lower_like)
        if tag_clauses:
            clauses.append("(" + " OR ".join(tag_clauses) + ")")
    if not clauses:
        return "", []
    return " AND (" + " OR ".join(clauses) + ")", params


async def _claim_pending_task(
    *,
    tenant_id: str,
    agent_name: str,
    project_id: str = "",
    tags: list[str] | None = None,
) -> dict[str, Any] | None:
    tags = tags or []
    await scheduler_tick_once(tenant_id=tenant_id)

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            task_cols = await get_table_columns_cached(cur, "tasks")
            tasks_has_tenant = "tenant_id" in task_cols

            where_parts = ["status = 'pending'"]
            params: list[Any] = []
            if tasks_has_tenant:
                where_parts.append("tenant_id = %s")
                params.append(tenant_id)
            if project_id and "project_id" in task_cols:
                where_parts.append("project_id = %s")
                params.append(project_id)
            if "assigned_agent" in task_cols:
                where_parts.append("(assigned_agent IS NULL OR assigned_agent = '' OR assigned_agent = %s)")
                params.append(agent_name)

            tag_sql, tag_params = _build_tag_filters(tags, task_cols)
            where_sql = " AND ".join(where_parts) + tag_sql
            params.extend(tag_params)

            await cur.execute(
                f"""
                SELECT *
                FROM tasks
                WHERE {where_sql}
                ORDER BY priority ASC NULLS LAST, created_at ASC NULLS LAST, updated_at ASC NULLS LAST
                LIMIT 1
                FOR UPDATE SKIP LOCKED
                """,
                tuple(params),
            )
            row = await cur.fetchone()
            if not row:
                return None

            row = _normalize_task_row(row, task_pk)
            set_parts = ["status = 'in-progress'", "updated_at = NOW()"]
            update_params: list[Any] = [agent_name]
            if "assigned_agent" in task_cols:
                set_parts.insert(0, "assigned_agent = %s")
            if "started_at" in task_cols:
                set_parts.append("started_at = COALESCE(started_at, NOW())")
            update_params.append(row["id"])
            if tasks_has_tenant:
                update_where = f"{task_pk} = %s AND tenant_id = %s"
                update_params.append(tenant_id)
            else:
                update_where = f"{task_pk} = %s"

            await cur.execute(
                f"UPDATE tasks SET {', '.join(set_parts)} WHERE {update_where}",
                tuple(update_params),
            )
            await insert_agent_event(
                cur,
                task_id=row["id"],
                event_type="claimed",
                tenant_id=tenant_id,
                agent_name=agent_name,
                payload={"project_id": row.get("project_id", ""), "source": "core_compat"},
            )
        await conn.commit()
    return row


class PollBody(BaseModel):
    agent_name: str = "unknown"
    claim_ttl_s: int = Field(120, ge=10, le=3600)
    timeout_s: int = Field(30, ge=1, le=120)


class ClaimBody(BaseModel):
    agent_name: str = Field(..., min_length=1)
    project_id: str = ""
    claim_ttl_s: int = Field(300, ge=10, le=3600)
    tags: list[str] = Field(default_factory=list)


class LegacyHeartbeatBody(BaseModel):
    agent: str = Field(..., min_length=1)
    progress: int = Field(0, ge=0, le=100)
    step: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class LegacyCompletionBody(BaseModel):
    task_id: str = Field(..., min_length=1)
    status: str = Field("success", min_length=1)
    summary: str = ""
    files_modified: list[str] = Field(default_factory=list)
    backend_used: str = "unknown"
    tests_passed: bool = True
    policy_violations: list[str] = Field(default_factory=list)
    next_suggested_tasks: list[str] = Field(default_factory=list)
    agent_name: str = ""


@router.post("/queue/poll")
async def core_queue_poll(
    body: PollBody,
    tenant_id: str = Depends(get_tenant_id),
):
    deadline = asyncio.get_running_loop().time() + body.timeout_s
    while True:
        task = await _claim_pending_task(
            tenant_id=tenant_id,
            agent_name=body.agent_name,
        )
        if task:
            return {"task_id": task["id"], "task": task}
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            return JSONResponse(content={"task_id": None}, status_code=204)
        await asyncio.sleep(min(2.0, remaining))


@router.post("/queue/release/{task_id}")
async def core_queue_release(task_id: str, tenant_id: str = Depends(get_tenant_id)):
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            task_cols = await get_table_columns_cached(cur, "tasks")
            tasks_has_tenant = "tenant_id" in task_cols
            set_parts = ["status = 'pending'", "updated_at = NOW()"]
            if "assigned_agent" in task_cols:
                set_parts.append("assigned_agent = NULL")
            await cur.execute(
                f"UPDATE tasks SET {', '.join(set_parts)} WHERE {task_pk} = %s"
                + (" AND tenant_id = %s" if tasks_has_tenant else ""),
                (task_id, tenant_id) if tasks_has_tenant else (task_id,),
            )
        await conn.commit()
    return {"released": task_id}


@router.post("/tasks/claim")
async def core_tasks_claim(
    body: ClaimBody,
    tenant_id: str = Depends(get_tenant_id),
):
    task = await _claim_pending_task(
        tenant_id=tenant_id,
        agent_name=body.agent_name,
        project_id=body.project_id,
        tags=body.tags,
    )
    return {"task": task}


@router.post("/tasks/{task_id}/heartbeat")
async def core_task_heartbeat(
    task_id: str,
    body: LegacyHeartbeatBody,
    tenant_id: str = Depends(get_tenant_id),
):
    hb = Heartbeat(
        task_id=task_id,
        progress_pct=body.progress,
        current_step=body.step,
        metadata=body.metadata,
    )
    return await agent_heartbeat_api(agent_name=body.agent, hb=hb, tenant_id=tenant_id)


@router.post("/tasks/complete")
async def core_tasks_complete(
    body: LegacyCompletionBody,
    background_tasks: BackgroundTasks,
    tenant_id: str = Depends(get_tenant_id),
):
    completion = Completion(
        task_id=body.task_id,
        status=_normalize_completion_status(body.status),
        summary=body.summary,
        files_modified=body.files_modified,
        tests_passed=body.tests_passed,
        policy_violations=body.policy_violations,
        next_suggested_tasks=body.next_suggested_tasks,
        backend_used=body.backend_used,
    )

    agent_name = body.agent_name.strip()
    if not agent_name:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                task_cols = await get_table_columns_cached(cur, "tasks")
                tasks_has_tenant = "tenant_id" in task_cols
                await cur.execute(
                    "SELECT assigned_agent FROM tasks WHERE "
                    + f"{task_pk} = %s"
                    + (" AND tenant_id = %s" if tasks_has_tenant else ""),
                    (body.task_id, tenant_id) if tasks_has_tenant else (body.task_id,),
                )
                row = await cur.fetchone()
                agent_name = str((row or {}).get("assigned_agent") or "external-agent")

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            result = await _apply_agent_completion(
                conn,
                cur,
                agent_name=agent_name,
                tenant_id=tenant_id,
                body=completion,
            )
            await conn.commit()

    await _publish_completion_audit(result["audit_event"])

    if result["files_modified"]:
        background_tasks.add_task(
            _sync_digital_twin,
            body.task_id,
            result["files_modified"],
            tenant_id,
            result["project_id"],
        )

    return {"ok": True, "task_status": result["task_status"], "unblocked": result["unblocked"]}


@router.get("/scheduler/status")
async def core_scheduler_status(
    request: Request,
    _tenant_id: str = Depends(get_tenant_id),
):
    bootstrap = getattr(request.app.state, "bootstrap_status", {})
    return {
        "scheduler": {"status": bootstrap.get("background_tasks", {}).get("scheduler_worker", "unknown")},
        "watchdog": {"status": bootstrap.get("background_tasks", {}).get("watchdog", "unknown")},
        "config": {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "source": "streaming",
        },
    }
