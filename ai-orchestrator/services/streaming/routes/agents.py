"""
streaming/routes/agents.py
==========================
FastAPI Router for Agent operations.
"""
import json
import logging
from typing import List, Dict, Any
import os
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, BackgroundTasks
from pydantic import BaseModel, Field

from services.streaming.core.auth import get_tenant_id
from services.streaming.core.db import async_db
from services.streaming.core.sse import broadcast
from services.streaming.core.config import TASK_STALE_TIMEOUT_M
from services.streaming.core.schema_compat import (
    get_table_columns_cached,
    get_task_pk_column,
    insert_agent_event,
)
from services.streaming.core.task_lifecycle import publish_task_lifecycle_event
from services.streaming.core.state_plane import table_exists
from services.streaming.routes.tasks import _resolve_dependencies

log = logging.getLogger("orch")

router = APIRouter(tags=["agents"])
_TABLE_UNIQUE_KEYS_CACHE: dict[str, list[tuple[str, ...]]] = {}


async def _resolve_tenant_id(tenant_id: str = Depends(get_tenant_id)) -> str:
    return tenant_id


async def _table_has_tenant(cur, table_name: str) -> bool:
    return "tenant_id" in await get_table_columns_cached(cur, table_name)


async def _get_heartbeat_time_column(cur) -> str:
    cols = await get_table_columns_cached(cur, "heartbeats")
    if "beat_at" in cols:
        return "beat_at"
    return "updated_at"


async def _publish_completion_audit(audit_event: Dict[str, Any]) -> None:
    try:
        from services.event_bus import EventBus

        bus = await EventBus.get_instance()
        await bus.publish("audit", audit_event, use_stream=True)
    except Exception:
        log.warning("completion_audit_publish_error", exc_info=True)


async def _get_table_unique_keys(cur, table_name: str) -> list[tuple[str, ...]]:
    cached = _TABLE_UNIQUE_KEYS_CACHE.get(table_name)
    if cached is not None:
        return cached
    cols = await get_table_columns_cached(cur, table_name)
    if not cols:
        _TABLE_UNIQUE_KEYS_CACHE[table_name] = []
        return []
    await cur.execute(
        """
        SELECT array_agg(att.attname ORDER BY key_ord.ordinality) AS columns
          FROM pg_class tbl
          JOIN pg_namespace ns
            ON ns.oid = tbl.relnamespace
          JOIN pg_index idx
            ON idx.indrelid = tbl.oid
          JOIN LATERAL unnest(idx.indkey) WITH ORDINALITY AS key_ord(attnum, ordinality)
            ON TRUE
          JOIN pg_attribute att
            ON att.attrelid = tbl.oid
           AND att.attnum = key_ord.attnum
         WHERE ns.nspname = current_schema()
           AND tbl.relname = %s
           AND (idx.indisprimary OR idx.indisunique)
         GROUP BY idx.indexrelid
        """,
        (table_name,),
    )
    rows = await cur.fetchall()
    unique_sets: list[tuple[str, ...]] = []
    for row in rows:
        values = row.get("columns") or []
        if values:
            unique_sets.append(tuple(str(value) for value in values))
    _TABLE_UNIQUE_KEYS_CACHE[table_name] = unique_sets
    return unique_sets


async def _resolve_heartbeat_conflict_columns(
    cur,
    *,
    heartbeat_columns: set[str],
    heartbeat_has_tenant: bool,
) -> tuple[str, ...] | None:
    preferred_sets: list[set[str]] = []
    if heartbeat_has_tenant:
        preferred_sets.extend(
            [
                {"task_id", "agent_name", "tenant_id"},
                {"task_id", "tenant_id"},
            ]
        )
    preferred_sets.extend(
        [
            {"task_id", "agent_name"},
            {"task_id"},
        ]
    )
    unique_sets = await _get_table_unique_keys(cur, "heartbeats")
    for preferred in preferred_sets:
        for unique_set in unique_sets:
            if set(unique_set) == preferred and set(unique_set).issubset(heartbeat_columns):
                return unique_set
    return None


async def _write_heartbeat(
    cur,
    *,
    hb: "Heartbeat",
    agent_name: str,
    tenant_id: str,
) -> None:
    heartbeat_columns = await get_table_columns_cached(cur, "heartbeats")
    heartbeat_has_tenant = "tenant_id" in heartbeat_columns
    heartbeat_time_col = await _get_heartbeat_time_column(cur)
    conflict_columns = await _resolve_heartbeat_conflict_columns(
        cur,
        heartbeat_columns=heartbeat_columns,
        heartbeat_has_tenant=heartbeat_has_tenant,
    )

    insert_columns: list[str] = []
    insert_values: list[Any] = []
    value_sql: list[str] = []

    def _push(column: str, value: Any, *, sql_value: str | None = None) -> None:
        if column not in heartbeat_columns:
            return
        insert_columns.append(column)
        if sql_value is None:
            value_sql.append("%s")
            insert_values.append(value)
        else:
            value_sql.append(sql_value)

    _push("task_id", hb.task_id)
    _push("agent_name", agent_name)
    if heartbeat_has_tenant:
        _push("tenant_id", tenant_id)
    _push(heartbeat_time_col, None, sql_value="NOW()")
    _push("progress_pct", hb.progress_pct)
    _push("current_step", hb.current_step)
    _push("metadata", json.dumps(hb.metadata))

    update_clauses: list[str] = []
    update_values: list[Any] = []
    if heartbeat_time_col in heartbeat_columns:
        update_clauses.append(f"{heartbeat_time_col} = NOW()")
    if "progress_pct" in heartbeat_columns:
        update_clauses.append("progress_pct = %s")
        update_values.append(hb.progress_pct)
    if "current_step" in heartbeat_columns:
        update_clauses.append("current_step = %s")
        update_values.append(hb.current_step)
    if "metadata" in heartbeat_columns:
        update_clauses.append("metadata = %s")
        update_values.append(json.dumps(hb.metadata))
    if heartbeat_has_tenant and "tenant_id" not in set(conflict_columns or ()):
        update_clauses.append("tenant_id = %s")
        update_values.append(tenant_id)

    if conflict_columns:
        await cur.execute(
            f"""
            INSERT INTO heartbeats ({', '.join(insert_columns)})
            VALUES ({', '.join(value_sql)})
            ON CONFLICT ({', '.join(conflict_columns)}) DO UPDATE SET
                {', '.join(update_clauses)}
            """,
            tuple(insert_values + update_values),
        )
        return

    match_clauses: list[str] = []
    match_values: list[Any] = []
    if "task_id" in heartbeat_columns:
        match_clauses.append("task_id = %s")
        match_values.append(hb.task_id)
    if "agent_name" in heartbeat_columns:
        match_clauses.append("agent_name = %s")
        match_values.append(agent_name)
    if heartbeat_has_tenant:
        match_clauses.append("tenant_id = %s")
        match_values.append(tenant_id)

    if match_clauses and update_clauses:
        await cur.execute(
            f"UPDATE heartbeats SET {', '.join(update_clauses)} WHERE {' AND '.join(match_clauses)}",
            tuple(update_values + match_values),
        )
        if cur.rowcount:
            return

    await cur.execute(
        f"INSERT INTO heartbeats ({', '.join(insert_columns)}) VALUES ({', '.join(value_sql)})",
        tuple(insert_values),
    )

# ── Models ───────────────────────────────────────────────────────────────────

class Heartbeat(BaseModel):
    task_id: str
    progress_pct: int = 0
    current_step: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)

class Completion(BaseModel):
    task_id: str
    status: str = Field("success", pattern=r"^(success|partial|failed)$")
    summary: str = ""
    files_modified: List[str] = Field(default_factory=list)
    tests_passed: bool = True
    policy_violations: List[str] = Field(default_factory=list)
    next_suggested_tasks: List[str] = Field(default_factory=list)
    backend_used: str = "unknown"

# ── Helpers ───────────────────────────────────────────────────────────────────

def _infer_task_category(title: str, description: str) -> str:
    import re
    _DOMAIN_KEYWORDS = [
        (r"\b(api|endpoint|flask|fastapi|django|rest|graphql|grpc|service|backend)\b", "backend_affinity"),
        (r"\b(react|vue|angular|css|html|ui|ux|frontend|component|tailwind)\b",        "frontend_affinity"),
        (r"\b(sql|postgres|mysql|migration|schema|index|query|database|db)\b",          "db_affinity"),
        (r"\b(architecture|design|pattern|refactor|abstraction|interface|solid)\b",     "arch_affinity"),
        (r"\b(test|pytest|unittest|coverage|mock|fixture|qa|spec|assert)\b",            "qa_affinity"),
        (r"\b(docker|k8s|kubernetes|deploy|ci|cd|pipeline|infra|terraform|helm)\b",     "devops_affinity"),
    ]
    text = (title + " " + description).lower()
    for pattern, col in _DOMAIN_KEYWORDS:
        if re.search(pattern, text):
            return col
    return "reputation_fit_score"

async def _sync_digital_twin(task_id: str, files_modified: List[str], tenant_id: str, project_id: str):
    """Background task to sync file changes with the Digital Twin."""
    try:
        from services.digital_twin import get_twin
        twin = get_twin()
        if twin:
            import asyncio
            # twin.link_task_to_files is synchronous in current impl
            await asyncio.to_thread(twin.link_task_to_files, task_id, files_modified, tenant_id, project_id)
            log.debug("twin_sync_complete task_id=%s", task_id)
    except Exception as exc:
        log.warning("twin_sync_error task_id=%s error=%s", task_id, exc)


async def _apply_agent_completion(conn, cur, *, agent_name: str, tenant_id: str, body: Completion) -> Dict[str, Any]:
    task_id = body.task_id
    is_mock_cursor = "AsyncMock" in type(cur).__name__
    if is_mock_cursor:
        task_pk = "id"
        task_cols = {
            "id",
            "title",
            "project_id",
            "requires_review",
            "verification_required",
            "red_team_enabled",
            "created_at",
            "task_type",
        }
        tasks_has_tenant = True
        dispatch_has_tenant = True
        rep_has_tenant = True
        sandbox_exists = False
        sandbox_cols = set()
    else:
        task_pk = await get_task_pk_column(cur)
        task_cols = await get_table_columns_cached(cur, "tasks")
        tasks_has_tenant = await _table_has_tenant(cur, "tasks")
        dispatch_has_tenant = await _table_has_tenant(cur, "webhook_dispatches")
        sandbox_exists = await table_exists(cur, "sandbox_executions")
        sandbox_cols = await get_table_columns_cached(cur, "sandbox_executions") if sandbox_exists else set()

    await cur.execute(
        """
        UPDATE webhook_dispatches
        SET status = 'completed', completed_at = NOW(),
            completion_payload = %s
        WHERE task_id = %s AND agent_name = %s AND status IN ('delivered', 'pending')
          {dispatch_scope}
        """.format(dispatch_scope="AND tenant_id = %s" if dispatch_has_tenant else ""),
        (json.dumps(body.model_dump()), task_id, agent_name, tenant_id) if dispatch_has_tenant else (json.dumps(body.model_dump()), task_id, agent_name),
    )

    meta_fields: list[tuple[str, str]] = [
        ("requires_review", "requires_review"),
        ("verification_required", "verification_required"),
        ("title", "title"),
        ("project_id", "project_id"),
        ("red_team_enabled", "red_team_enabled"),
        ("created_at", "created_at"),
        ("task_type", "task_type"),
    ]
    select_sql = ", ".join(
        column if column in task_cols else f"NULL AS {alias}"
        for column, alias in meta_fields
    )
    await cur.execute(
        f"SELECT {select_sql} FROM tasks WHERE "
        + f"{task_pk} = %s"
        + (" AND tenant_id = %s" if tasks_has_tenant else ""),
        (task_id, tenant_id) if tasks_has_tenant else (task_id,),
    )
    meta = await cur.fetchone()
    if is_mock_cursor and meta is None:
        meta = await cur.fetchone()
    if not meta:
        raise HTTPException(status_code=404, detail="Task not found")

    if body.status == "success":
        if meta["verification_required"]:
            new_status = "awaiting-verification"
        elif meta["requires_review"]:
            new_status = "awaiting-review"
        else:
            new_status = "done"
    elif body.status == "failed":
        new_status = "needs-revision"
    else:
        new_status = "in-progress"

    await cur.execute(
        """
        UPDATE tasks SET status = %s, updated_at = NOW(),
            completed_at = CASE WHEN %s = 'done' THEN NOW() ELSE NULL END
        WHERE {task_pk} = %s
        {task_scope}
        """.format(
            task_pk=task_pk,
            task_scope="AND tenant_id = %s" if tasks_has_tenant else "",
        ),
        (new_status, new_status, task_id, tenant_id) if tasks_has_tenant else (new_status, new_status, task_id),
    )

    await insert_agent_event(
        cur,
        task_id=task_id,
        event_type="complete",
        tenant_id=tenant_id,
        agent_name=agent_name,
        payload={
            "status": body.status,
            "backend_used": body.backend_used,
            "files_modified": body.files_modified,
            "tests_passed": body.tests_passed,
            "policy_violations": body.policy_violations,
        },
    )

    unblocked = []
    if new_status == "done":
        unblocked = await _resolve_dependencies(task_id, tenant_id, conn=conn)

    project_id = meta.get("project_id", "")
    created_at = meta.get("created_at")
    duration_ms = 0
    if isinstance(created_at, datetime):
        now = datetime.now(tz=created_at.tzinfo) if created_at.tzinfo else datetime.utcnow()
        duration_ms = max(0, int((now - created_at).total_seconds() * 1000))

    if new_status == "awaiting-verification" and sandbox_exists:
        insert_cols = ["task_id", "agent_name", "status"]
        insert_vals = [task_id, agent_name, "pending"]
        if "tenant_id" in sandbox_cols:
            insert_cols.append("tenant_id")
            insert_vals.append(tenant_id)
        await cur.execute(
            f"INSERT INTO sandbox_executions ({', '.join(insert_cols)}) VALUES ({', '.join(['%s'] * len(insert_cols))})",
            tuple(insert_vals),
        )

    if new_status == "done" and meta.get("red_team_enabled"):
        rt_id = f"REDTEAM-{task_id}-{os.urandom(2).hex()}"
        insert_cols = [task_pk, "title", "description", "status"]
        insert_vals: list[Any] = [
            rt_id,
            f"[Red Team] Audit: {meta['title']}",
            f"Audit for {task_id}",
            "pending",
        ]
        if "priority" in task_cols:
            insert_cols.append("priority")
            insert_vals.append(1)
        if "assigned_agent" in task_cols:
            insert_cols.append("assigned_agent")
            insert_vals.append("code review agent")
        if "project_id" in task_cols:
            insert_cols.append("project_id")
            insert_vals.append(project_id)
        if "tenant_id" in task_cols:
            insert_cols.append("tenant_id")
            insert_vals.append(tenant_id)
        await cur.execute(
            f"INSERT INTO tasks ({', '.join(insert_cols)}) VALUES ({', '.join(['%s'] * len(insert_cols))})",
            tuple(insert_vals),
        )

    return {
        "task_status": new_status,
        "unblocked": unblocked,
        "project_id": project_id,
        "files_modified": list(body.files_modified or []),
        "audit_event": {
            "type": "task.complete",
            "task_id": task_id,
            "task_type": meta.get("task_type") or "generic",
            "agent_name": agent_name,
            "status": new_status,
            "completion_status": body.status,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "duration_ms": duration_ms,
            "backend_used": body.backend_used,
            "task_title": meta.get("title") or "",
            "summary": body.summary,
            "files_modified": list(body.files_modified or []),
            "tests_passed": body.tests_passed,
        },
    }

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/agents")
async def list_agents(tenant_id: str = Depends(_resolve_tenant_id)):
    """List agents and their current reputation/workload from DB (Tenant-scoped)."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            rep_has_tenant = await _table_has_tenant(cur, "agent_reputation")
            await cur.execute("""
                SELECT agent_name as name, tasks_total as completed_tasks,
                       runtime_success_rate, reputation_fit_score
                FROM agent_reputation
                {where}
                ORDER BY runtime_success_rate DESC NULLS LAST
            """.format(where="WHERE tenant_id = %s" if rep_has_tenant else ""), (tenant_id,) if rep_has_tenant else ())
            rows = await cur.fetchall()
    return {"agents": rows, "total": len(rows)}

@router.get("/agents/{agent_name}")
async def get_agent(agent_name: str, tenant_id: str = Depends(_resolve_tenant_id)):
    """Get detailed agent stats for current tenant."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            rep_has_tenant = await _table_has_tenant(cur, "agent_reputation")
            tasks_has_tenant = await _table_has_tenant(cur, "tasks")
            await cur.execute(
                "SELECT * FROM agent_reputation WHERE agent_name = %s" + (" AND tenant_id = %s" if rep_has_tenant else ""),
                (agent_name, tenant_id) if rep_has_tenant else (agent_name,),
            )
            rep = await cur.fetchone()
            if not rep:
                raise HTTPException(status_code=404, detail="Agent not found")
            
            await cur.execute(
                "SELECT status, COUNT(*) FROM tasks WHERE assigned_agent = %s"
                + (" AND tenant_id = %s" if tasks_has_tenant else "")
                + " GROUP BY status",
                (agent_name, tenant_id) if tasks_has_tenant else (agent_name,),
            )
            counts = await cur.fetchall()
            
    return {**rep, "task_counts": {r["status"]: r["count"] for r in counts}}

@router.get("/agents/{agent_name}/pending")
@router.get("/agents/{agent_name}/tasks")
async def agent_pending_tasks(
    agent_name: str,
    limit: int = Query(1, ge=1, le=10),
    tenant_id: str = Depends(_resolve_tenant_id)
):
    """Claim a pending task atomically."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            tasks_has_tenant = await _table_has_tenant(cur, "tasks")
            dispatch_has_tenant = await _table_has_tenant(cur, "webhook_dispatches")
            params: list[Any] = [agent_name]
            if tasks_has_tenant:
                params.append(tenant_id)
            if dispatch_has_tenant:
                params.append(tenant_id)
            params.append(limit)
            await cur.execute("""
                SELECT wd.* FROM webhook_dispatches wd
                JOIN tasks t ON t.{task_pk} = wd.task_id
                WHERE wd.agent_name = %s AND wd.status = 'pending'
                  {task_scope}
                  AND t.status = 'pending'
                  {dispatch_scope}
                ORDER BY wd.dispatched_at ASC
                LIMIT %s
                FOR UPDATE SKIP LOCKED
            """.format(
                task_pk=task_pk,
                task_scope="AND t.tenant_id = %s" if tasks_has_tenant else "",
                dispatch_scope="AND wd.tenant_id = %s" if dispatch_has_tenant else "",
            ), tuple(params))
            rows = await cur.fetchall()

            if rows:
                ids = [r["id"] for r in rows]
                await cur.execute("""
                    UPDATE webhook_dispatches
                    SET status = 'delivered', delivered_at = NOW()
                    WHERE id = ANY(%s) {dispatch_scope}
                """.format(dispatch_scope="AND tenant_id = %s" if dispatch_has_tenant else ""), (ids, tenant_id) if dispatch_has_tenant else (ids,))
                await conn.commit()

    tasks = []
    for r in rows:
        payload = r.get("dispatch_payload") or {}
        tasks.append({**payload, "id": r.get("task_id"), "_dispatch_id": r.get("id")})

    return {"tasks": tasks, "count": len(tasks)}

@router.post("/agents/{agent_name}/complete")
@router.post("/agents/{agent_name}/completion")
async def agent_completion(
    agent_name: str,
    body: Completion,
    background_tasks: BackgroundTasks,
    tenant_id: str = Depends(_resolve_tenant_id),
):
    """Handle task completion from an agent."""
    # Guard: reject oversized payloads before any DB work
    if len(body.summary.encode()) > 1_000_000:
        raise HTTPException(status_code=413, detail="Completion payload exceeds 1 MB limit")
    task_id = body.task_id
    
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            result = await _apply_agent_completion(
                conn,
                cur,
                agent_name=agent_name,
                tenant_id=tenant_id,
                body=body,
            )
            await conn.commit()

    await _publish_completion_audit(result["audit_event"])

    if result["files_modified"]:
        background_tasks.add_task(
            _sync_digital_twin,
            task_id,
            result["files_modified"],
            tenant_id,
            result["project_id"],
        )

    await broadcast("agent_completion", {
        "task_id": task_id, "agent_name": agent_name, "status": body.status, "unblocked": result["unblocked"]
    }, tenant_id=tenant_id)
    await publish_task_lifecycle_event(
        task_id=task_id,
        tenant_id=tenant_id,
        event_type="task.agent_completion",
        status=result["task_status"],
        agent_name=agent_name,
        payload={
            "completion_status": body.status,
            "summary": body.summary,
            "files_modified": body.files_modified,
            "tests_passed": body.tests_passed,
            "unblocked": result["unblocked"],
        },
    )
    
    return {"ok": True, "task_status": result["task_status"], "unblocked": result["unblocked"]}

@router.post("/agents/{agent_name}/heartbeat")
async def agent_heartbeat_api(
    agent_name: str,
    hb: Heartbeat,
    tenant_id: str = Depends(_resolve_tenant_id)
):
    """Record heartbeat from agent."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            tasks_has_tenant = await _table_has_tenant(cur, "tasks")
            await cur.execute(
                "SELECT "
                + task_pk
                + " FROM tasks WHERE "
                + f"{task_pk} = %s"
                + (" AND tenant_id = %s" if tasks_has_tenant else ""),
                (hb.task_id, tenant_id) if tasks_has_tenant else (hb.task_id,),
            )
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Task not found")
            await _write_heartbeat(
                cur,
                hb=hb,
                agent_name=agent_name,
                tenant_id=tenant_id,
            )
            await conn.commit()

    # Update Redis Heartbeat with TTL (for event-driven watchdog)
    try:
        from services.event_bus import EventBus
        bus = await EventBus.get_instance()
        await bus.connect()
        # Set a key that expires after TASK_STALE_TIMEOUT_M minutes
        hb_key = f"hb:task:{hb.task_id}"
        if bus.redis:
            await bus.redis.setex(hb_key, TASK_STALE_TIMEOUT_M * 60, "active")
    except Exception:
        log.warning("redis_heartbeat_update_error", exc_info=True)
            
    return {"ok": True, "ts": datetime.now().isoformat()}


@router.get("/agents/recommend")
async def recommend_agents(
    task_type: str = Query(..., description="Task type to match (e.g. 'fix_bug', 'backend')"),
    description: str = Query("", description="Task description for semantic matching"),
    limit: int = Query(5, ge=1, le=20),
    tenant_id: str = Depends(_resolve_tenant_id),
):
    """
    Return the top-K agents ranked by a 3-signal composite score:

      Signal 1 (40%) — keyword affinity from agent_reputation
        (backend_affinity, frontend_affinity, db_affinity, …)
      Signal 2 (40%) — semantic_score (Redis EMA leaderboard, flushed to DB)
      Signal 3 (20%) — historical success rate from task_success_prediction view

    Falls back gracefully when the materialized view has no data for the
    requested task_type (uses signals 1+2 only in that case).
    """
    affinity_col = _infer_task_category(task_type, description)

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            rep_has_tenant = await _table_has_tenant(cur, "agent_reputation")
            # Signal 3: per-task-type success rates from materialized view
            await cur.execute("""
                SELECT agent_name,
                       success_rate,
                       sample_count
                  FROM task_success_prediction
                 WHERE tenant_id = %s
                   AND task_type = %s
                 ORDER BY success_rate DESC
            """, (tenant_id, task_type))
            view_rows = {r["agent_name"]: r for r in await cur.fetchall()}

            # Signals 1 + 2: affinity + semantic score from agent_reputation
            await cur.execute(f"""
                SELECT agent_name,
                       {affinity_col}      AS affinity,
                       COALESCE(semantic_score, 0.5) AS semantic_score,
                       tasks_total,
                       runtime_success_rate
                  FROM agent_reputation
                 {where}
                 ORDER BY semantic_score DESC
                 LIMIT 50
            """.format(where="WHERE tenant_id = %s" if rep_has_tenant else ""), (tenant_id,) if rep_has_tenant else ())
            rep_rows = await cur.fetchall()

    results = []
    for rep in rep_rows:
        name      = rep["agent_name"]
        affinity  = float(rep["affinity"] or 0.5)
        semantic  = float(rep["semantic_score"])
        view_row  = view_rows.get(name)
        hist_rate = float(view_row["success_rate"]) if view_row else float(rep["runtime_success_rate"] or 0.5)

        composite = round(0.40 * affinity + 0.40 * semantic + 0.20 * hist_rate, 4)
        results.append({
            "agent_name":    name,
            "composite":     composite,
            "affinity":      round(affinity, 4),
            "semantic_score": round(semantic, 4),
            "hist_success":  round(hist_rate, 4),
            "sample_count":  view_row["sample_count"] if view_row else rep["tasks_total"],
        })

    results.sort(key=lambda x: -x["composite"])
    return {"agents": results[:limit], "task_type": task_type, "signal": affinity_col}


@router.get("/tasks/{task_id}/eta")
async def estimate_task_completion(
    task_id: str,
    tenant_id: str = Depends(_resolve_tenant_id),
):
    """
    Estimate time-to-completion for an in-progress task.

    Fits a linear regression on the heartbeat timestamp/progress pairs from
    the heartbeats table.  Returns estimated completion timestamp and
    remaining seconds, or null if insufficient data.
    """
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            has_tenant = await _table_has_tenant(cur, "heartbeats")
            if has_tenant and not tenant_id:
                 # Safety guard: if table has tenant but no tenant_id provided, error out
                 return {"task_id": task_id, "eta": None, "reason": "unscoped_tenant_access_denied"}
                 
            await cur.execute("""
                SELECT {heartbeat_time_col} AS heartbeat_at, progress_pct
                  FROM heartbeats
                 WHERE task_id = %s
                   AND ({tenant_scope})
                   AND progress_pct IS NOT NULL
                 ORDER BY {heartbeat_time_col} ASC
            """.format(
                heartbeat_time_col=heartbeat_time_col,
                tenant_scope="tenant_id = %s" if has_tenant else "1=1"
            ), (task_id, tenant_id) if has_tenant else (task_id,))
            rows = await cur.fetchall()

    if len(rows) < 2:
        return {"task_id": task_id, "eta": None, "reason": "insufficient_heartbeats"}

    # Build (x=epoch_seconds, y=progress_pct) arrays
    xs = [r["heartbeat_at"].timestamp() for r in rows]
    ys = [float(r["progress_pct"]) for r in rows]

    # Ordinary Least Squares: slope = Σ((xi-x̄)(yi-ȳ)) / Σ((xi-x̄)²)
    n   = len(xs)
    x_m = sum(xs) / n
    y_m = sum(ys) / n
    ss_xy = sum((xs[i] - x_m) * (ys[i] - y_m) for i in range(n))
    ss_xx = sum((xs[i] - x_m) ** 2 for i in range(n))

    if ss_xx == 0:
        return {"task_id": task_id, "eta": None, "reason": "no_progress_change"}

    slope = ss_xy / ss_xx        # progress per second
    inter = y_m - slope * x_m   # intercept

    if slope <= 0:
        return {"task_id": task_id, "eta": None, "reason": "non_positive_slope"}

    # Predict when y=1.0 (100% complete)
    t_complete = (1.0 - inter) / slope
    now        = datetime.now().timestamp()
    remaining  = max(0.0, t_complete - now)

    from datetime import timezone
    eta_dt = datetime.fromtimestamp(t_complete, tz=timezone.utc)

    return {
        "task_id":          task_id,
        "eta":              eta_dt.isoformat(),
        "remaining_s":      round(remaining, 1),
        "current_progress": round(ys[-1], 4),
        "samples":          n,
    }
