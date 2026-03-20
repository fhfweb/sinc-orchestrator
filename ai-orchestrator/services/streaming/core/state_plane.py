from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from .auth import now_iso
from .config import (
    AGENTS_WORKLOAD,
    HEALTH_REPORT,
    TASKS_DAG,
    WHITEBOARD,
    TASK_STALE_TIMEOUT_M,
    env_get_int,
)
from .db import async_db

_STATE_PLANE_DB_TIMEOUT_S = 3.0
_TASK_DAG_PROJECTION_INTERVAL_S = env_get_int(
    "ORCHESTRATOR_TASK_DAG_PROJECTION_INTERVAL_SECONDS",
    default=20,
)
log = logging.getLogger("orchestrator.state-plane")


def _read_json_projection(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json_projection(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    temp_path.replace(path)


async def table_exists(cur, table_name: str) -> bool:
    await cur.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (table_name,))
    row = await cur.fetchone()
    return bool(row and row["present"])


async def table_columns(cur, table_name: str) -> set[str]:
    await cur.execute(
        """
        SELECT column_name
          FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name = %s
        """,
        (table_name,),
    )
    rows = await cur.fetchall()
    return {str(row["column_name"]) for row in rows}


async def ensure_whiteboard_schema() -> None:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS whiteboard_entries (
                    whiteboard_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    task_id TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    intention TEXT,
                    files_intended JSONB DEFAULT '[]'::jsonb,
                    status TEXT NOT NULL,
                    announced_at TIMESTAMPTZ DEFAULT NOW(),
                    completed_at TIMESTAMPTZ,
                    handoff_to TEXT,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                "ALTER TABLE whiteboard_entries ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT ''"
            )
            await cur.execute(
                "ALTER TABLE whiteboard_entries ADD COLUMN IF NOT EXISTS files_intended JSONB DEFAULT '[]'::jsonb"
            )
            await cur.execute(
                "ALTER TABLE whiteboard_entries ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()"
            )
            await cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_whiteboard_entries_tenant_task
                    ON whiteboard_entries (tenant_id, task_id)
                """
            )
        await conn.commit()


def _normalize_whiteboard_rows(rows: list[dict[str, Any]]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    latest_updated = ""
    for row in rows:
        announced_at = str(row.get("announced_at") or "")
        updated_at = str(row.get("updated_at") or row.get("announced_at") or "")
        latest_updated = max(latest_updated, updated_at)
        files_intended = row.get("files_intended")
        if isinstance(files_intended, str):
            try:
                files_intended = json.loads(files_intended)
            except json.JSONDecodeError:
                files_intended = []
        entries.append(
            {
                "task_id": row.get("task_id", ""),
                "agent": row.get("agent_name", ""),
                "intention": row.get("intention", ""),
                "files_intended": files_intended or [],
                "status": row.get("status", "announced"),
                "announced_at": announced_at,
                "completed_at": str(row.get("completed_at") or ""),
                "handoff_to": row.get("handoff_to", "") or "",
            }
        )
    return {
        "entries": entries,
        "updated_at": latest_updated or now_iso(),
        "source": "db",
    }


async def get_whiteboard_snapshot(tenant_id: str) -> dict[str, Any]:
    async def _load_from_db() -> dict[str, Any]:
        await ensure_whiteboard_schema()
        async with async_db(bypass_rls=True) as conn:
            async with conn.cursor() as cur:
                if not await table_exists(cur, "whiteboard_entries"):
                    raise RuntimeError("whiteboard_entries_table_missing")
                columns = await table_columns(cur, "whiteboard_entries")
                if "tenant_id" in columns:
                    await cur.execute(
                        """
                        SELECT tenant_id, task_id, agent_name, intention, files_intended,
                               status, announced_at, completed_at, handoff_to, updated_at
                          FROM whiteboard_entries
                         WHERE tenant_id = %s
                         ORDER BY announced_at DESC, whiteboard_id DESC
                        """,
                        (tenant_id,),
                    )
                else:
                    await cur.execute(
                        """
                        SELECT NULL::text AS tenant_id, task_id, agent_name, intention, files_intended,
                               status, announced_at, completed_at, handoff_to, announced_at AS updated_at
                          FROM whiteboard_entries
                         ORDER BY announced_at DESC, task_id DESC
                        """
                    )
                rows = await cur.fetchall()
        snapshot = _normalize_whiteboard_rows(rows)
        _write_json_projection(WHITEBOARD, snapshot)
        return snapshot

    try:
        return await asyncio.wait_for(_load_from_db(), timeout=_STATE_PLANE_DB_TIMEOUT_S)
    except Exception:
        snapshot = _read_json_projection(WHITEBOARD, {"entries": []})
        snapshot["source"] = "projection"
        return snapshot


async def announce_whiteboard_entry(
    *,
    tenant_id: str,
    task_id: str,
    agent_name: str,
    intention: str,
    files_intended: list[str],
) -> dict[str, Any]:
    async def _write_to_db() -> dict[str, Any]:
        await ensure_whiteboard_schema()
        async with async_db(bypass_rls=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO whiteboard_entries (
                        tenant_id, task_id, agent_name, intention,
                        files_intended, status, announced_at, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s::jsonb, 'announced', NOW(), NOW())
                    ON CONFLICT (tenant_id, task_id) DO UPDATE SET
                        agent_name = EXCLUDED.agent_name,
                        intention = EXCLUDED.intention,
                        files_intended = EXCLUDED.files_intended,
                        status = EXCLUDED.status,
                        completed_at = NULL,
                        handoff_to = NULL,
                        updated_at = NOW()
                    RETURNING tenant_id, task_id, agent_name, intention, files_intended,
                              status, announced_at, completed_at, handoff_to, updated_at
                    """,
                    (
                        tenant_id,
                        task_id,
                        agent_name,
                        intention,
                        json.dumps(files_intended or []),
                    ),
                )
                row = await cur.fetchone()
            await conn.commit()
        return row

    row = await asyncio.wait_for(_write_to_db(), timeout=_STATE_PLANE_DB_TIMEOUT_S)
    snapshot = await get_whiteboard_snapshot(tenant_id)
    for entry in snapshot.get("entries", []):
        if entry.get("task_id") == task_id:
            return entry
    return _normalize_whiteboard_rows([row]).get("entries", [{}])[0]


async def get_system_status_snapshot(tenant_id: str) -> dict[str, Any]:
    async def _load_from_db() -> dict[str, Any]:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                tasks_total = 0
                by_status: dict[str, int] = {}
                if await table_exists(cur, "tasks"):
                    task_columns = await table_columns(cur, "tasks")
                    await cur.execute(
                        (
                            """
                            SELECT status, COUNT(*) AS n
                              FROM tasks
                             WHERE tenant_id = %s
                             GROUP BY status
                            """
                            if "tenant_id" in task_columns
                            else """
                            SELECT status, COUNT(*) AS n
                              FROM tasks
                             GROUP BY status
                            """
                        ),
                        (tenant_id,) if "tenant_id" in task_columns else (),
                    )
                    rows = await cur.fetchall()
                    by_status = {str(row["status"]): int(row["n"]) for row in rows}
                    tasks_total = sum(by_status.values())

                loop = {}
                if await table_exists(cur, "loop_states"):
                    loop_columns = await table_columns(cur, "loop_states")
                    has_loop_tenant = "tenant_id" in loop_columns
                    await cur.execute(
                        (
                            """
                            SELECT cycle, updated_at, status, phase, project_id
                              FROM loop_states
                             WHERE tenant_id = %s
                             ORDER BY updated_at DESC
                             LIMIT 1
                            """
                            if has_loop_tenant
                            else """
                            SELECT cycle, updated_at, status, phase, project_id
                              FROM loop_states
                             ORDER BY updated_at DESC
                             LIMIT 1
                            """
                        ),
                        (tenant_id,) if has_loop_tenant else (),
                    )
                    row = await cur.fetchone()
                    loop = row or {}

                active_agents = 0
                agents_total = 0
                if await table_exists(cur, "heartbeats"):
                    heartbeat_columns = await table_columns(cur, "heartbeats")
                    has_heartbeat_tenant = "tenant_id" in heartbeat_columns
                    await cur.execute(
                        (
                            """
                            SELECT
                                COUNT(DISTINCT agent_name) AS active_agents
                              FROM heartbeats
                             WHERE tenant_id = %s
                               AND beat_at > NOW() - (%s * INTERVAL '1 minute')
                            """
                            if has_heartbeat_tenant
                            else """
                            SELECT
                                COUNT(DISTINCT agent_name) AS active_agents
                              FROM heartbeats
                             WHERE beat_at > NOW() - (%s * INTERVAL '1 minute')
                            """
                        ),
                        (tenant_id, TASK_STALE_TIMEOUT_M)
                        if has_heartbeat_tenant
                        else (TASK_STALE_TIMEOUT_M,),
                    )
                    row = await cur.fetchone()
                    active_agents = int((row or {}).get("active_agents") or 0)
                    await cur.execute(
                        (
                            """
                            SELECT COUNT(DISTINCT agent_name) AS agents_total
                              FROM heartbeats
                             WHERE tenant_id = %s
                            """
                            if has_heartbeat_tenant
                            else """
                            SELECT COUNT(DISTINCT agent_name) AS agents_total
                              FROM heartbeats
                            """
                        ),
                        (tenant_id,) if has_heartbeat_tenant else (),
                    )
                    row = await cur.fetchone()
                    agents_total = int((row or {}).get("agents_total") or 0)

                policy_status = "unknown"
                policy_violations = 0
                if await table_exists(cur, "policy_reports"):
                    policy_columns = await table_columns(cur, "policy_reports")
                    has_policy_tenant = "tenant_id" in policy_columns
                    await cur.execute(
                        (
                            """
                            SELECT status, violations
                              FROM policy_reports
                             WHERE tenant_id = %s
                             ORDER BY created_at DESC
                             LIMIT 1
                            """
                            if has_policy_tenant
                            else """
                            SELECT status, violations
                              FROM policy_reports
                             ORDER BY created_at DESC
                             LIMIT 1
                            """
                        ),
                        (tenant_id,) if has_policy_tenant else (),
                    )
                    row = await cur.fetchone()
                    if row:
                        policy_status = str(row.get("status") or "unknown")
                        policy_violations = int(row.get("violations") or 0)

        health = "ok"
        if policy_status not in ("ok", "unknown") or policy_violations > 0:
            health = "degraded"
        if by_status.get("failed", 0) > 0 or by_status.get("needs-revision", 0) > 0:
            health = "degraded"
        if not loop and tasks_total == 0 and agents_total == 0:
            health = "unknown"

        return {
            "status": "ok",
            "health": health,
            "loop": {
                "cycle": int(loop.get("cycle") or 0),
                "last_heartbeat": str(loop.get("updated_at") or ""),
                "status": str(loop.get("status") or "unknown"),
            },
            "tasks": {"total": tasks_total, "by_status": by_status},
            "agents": {"total": agents_total, "active": active_agents},
            "policy": {"status": policy_status, "violations": policy_violations},
            "source": "db",
            "ts": now_iso(),
        }
    try:
        return await asyncio.wait_for(_load_from_db(), timeout=_STATE_PLANE_DB_TIMEOUT_S)
    except Exception:
        health = _read_json_projection(HEALTH_REPORT, {})
        dag = _read_json_projection(TASKS_DAG, {"tasks": []})
        workload = _read_json_projection(AGENTS_WORKLOAD, {"agents": []})
        by_status: dict[str, int] = {}
        for task in dag.get("tasks", []):
            status = str(task.get("status") or "unknown")
            by_status[status] = by_status.get(status, 0) + 1
        active = sum(1 for agent in workload.get("agents", []) if agent.get("active_tasks", 0) > 0)
        return {
            "status": "ok",
            "health": health.get("health_status", "unknown"),
            "loop": health.get("loop", {"cycle": 0, "last_heartbeat": "", "status": "unknown"}),
            "tasks": {"total": len(dag.get("tasks", [])), "by_status": by_status},
            "agents": {"total": len(workload.get("agents", [])), "active": active},
            "source": "projection",
            "ts": now_iso(),
        }


def _normalize_task_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            loaded = json.loads(value)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            return {}
    return {}


async def build_task_dag_projection(
    tenant_id: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    from .schema_compat import get_dependency_ref_column, get_task_pk_column

    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            if not await table_exists(cur, "tasks"):
                return {
                    "schema_version": "projection-v1",
                    "source": "db-projection",
                    "read_only": True,
                    "generated_at": now_iso(),
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "tasks": [],
                    "counts": {"total": 0, "by_status": {}},
                }

            task_pk = await get_task_pk_column(cur)
            dep_col = await get_dependency_ref_column(cur)
            task_columns = await table_columns(cur, "tasks")

            clauses: list[str] = []
            params: list[Any] = []
            if tenant_id and "tenant_id" in task_columns:
                clauses.append("t.tenant_id = %s")
                params.append(tenant_id)
            if project_id and "project_id" in task_columns:
                clauses.append("t.project_id = %s")
                params.append(project_id)
            where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""

            await cur.execute(
                f"""
                SELECT
                    t.*,
                    array_agg(d.{dep_col}) FILTER (WHERE d.{dep_col} IS NOT NULL) AS depends_on
                FROM tasks t
                LEFT JOIN dependencies d ON d.task_id = t.{task_pk}
                {where_sql}
                GROUP BY t.{task_pk}
                ORDER BY
                    CAST(t.priority AS TEXT) ASC NULLS LAST,
                    t.created_at ASC NULLS LAST,
                    t.updated_at ASC NULLS LAST
                """,
                tuple(params),
            )
            rows = await cur.fetchall()

    tasks: list[dict[str, Any]] = []
    by_status: dict[str, int] = {}
    for row in rows:
        metadata = _normalize_task_metadata(row.get("metadata"))
        task_id = str(row.get(task_pk) or "")
        task_payload = {
            "id": task_id,
            "task_id": task_id,
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "status": row.get("status") or "unknown",
            "priority": row.get("priority"),
            "assigned_agent": row.get("assigned_agent") or "",
            "project_id": row.get("project_id") or "",
            "tenant_id": row.get("tenant_id") or "",
            "depends_on": list(row.get("depends_on") or []),
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "completed_at": str(row.get("completed_at") or ""),
            "requires_review": bool(row.get("requires_review", False)),
            "verification_required": bool(row.get("verification_required", False)),
            "verification_script": row.get("verification_script") or "",
            "red_team_enabled": bool(row.get("red_team_enabled", False)),
            "plan_id": row.get("plan_id") or "",
            "metadata": metadata,
        }
        tasks.append(task_payload)
        status = str(task_payload["status"])
        by_status[status] = by_status.get(status, 0) + 1

    return {
        "schema_version": "projection-v1",
        "source": "db-projection",
        "read_only": True,
        "generated_at": now_iso(),
        "tenant_id": tenant_id,
        "project_id": project_id,
        "tasks": tasks,
        "counts": {
            "total": len(tasks),
            "by_status": by_status,
        },
    }


async def sync_task_dag_projection(
    tenant_id: str = "",
    project_id: str = "",
) -> dict[str, Any]:
    snapshot = await build_task_dag_projection(tenant_id=tenant_id, project_id=project_id)
    _write_json_projection(TASKS_DAG, snapshot)
    return snapshot


async def run_task_dag_projection_loop(
    tenant_id: str = "",
    project_id: str = "",
) -> None:
    log.info(
        "task_dag_projection_worker_started tenant=%s project=%s interval=%s",
        tenant_id or "*",
        project_id or "*",
        _TASK_DAG_PROJECTION_INTERVAL_S,
    )
    while True:
        try:
            await sync_task_dag_projection(tenant_id=tenant_id, project_id=project_id)
        except Exception as exc:
            log.exception("task_dag_projection_worker_error error=%s", exc)
        await asyncio.sleep(_TASK_DAG_PROJECTION_INTERVAL_S)
