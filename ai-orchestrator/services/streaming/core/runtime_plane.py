from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from .auth import now_iso
from .config import TASK_STALE_TIMEOUT_M
from .db import async_db
from .schema_compat import (
    get_table_columns_cached,
    get_dependency_ref_column,
    get_task_pk_column,
    insert_agent_event,
    table_has_column,
)
from .state_plane import table_exists

log = logging.getLogger("orchestrator.runtime")

SCHEDULER_INTERVAL_S = int(env_get("ORCHESTRATOR_SCHEDULER_INTERVAL_SECONDS", default="30"))
OBSERVER_INTERVAL_S = int(env_get("ORCHESTRATOR_OBSERVER_INTERVAL_SECONDS", default="45"))
INCIDENT_COOLDOWN_S = int(env_get("ORCHESTRATOR_INCIDENT_COOLDOWN_SECONDS", default="900"))


async def _get_cognitive_quality_snapshot() -> dict[str, Any]:
    try:
        from services.cognitive_orchestrator import get_cognitive_capability_snapshot_async

        return await get_cognitive_capability_snapshot_async(force_init=True)
    except Exception as exc:
        return {
            "initialized": False,
            "init_attempted": False,
            "quality_status": "unavailable",
            "score": 0.0,
            "critical_missing": ["cognitive_orchestrator"],
            "optional_missing": [],
            "components": {},
            "summary": f"snapshot unavailable: {exc}",
        }


async def ensure_runtime_plane_schema() -> None:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS loop_states (
                    loop_state_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    cycle BIGINT NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'idle',
                    summary TEXT NOT NULL DEFAULT '',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS cycle BIGINT NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS phase TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'idle'")
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS metadata JSONB NOT NULL DEFAULT '{}'::jsonb")
            await cur.execute("ALTER TABLE loop_states ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            await cur.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_loop_states_tenant_project
                    ON loop_states (tenant_id, project_id)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS policy_reports (
                    policy_report_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    report JSONB NOT NULL DEFAULT '{}'::jsonb,
                    violations INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'unknown',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute("ALTER TABLE policy_reports ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE policy_reports ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE policy_reports ADD COLUMN IF NOT EXISTS report JSONB NOT NULL DEFAULT '{}'::jsonb")
            await cur.execute("ALTER TABLE policy_reports ADD COLUMN IF NOT EXISTS violations INTEGER NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE policy_reports ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'unknown'")
            await cur.execute("ALTER TABLE policy_reports ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_policy_reports_tenant_created
                    ON policy_reports (tenant_id, created_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    project_id TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warning',
                    fingerprint TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    task_id TEXT,
                    source TEXT NOT NULL DEFAULT 'observer-worker',
                    status TEXT NOT NULL DEFAULT 'open',
                    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    resolved_at TIMESTAMPTZ,
                    resolution_reason TEXT
                )
                """
            )
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS project_id TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS category TEXT NOT NULL DEFAULT 'runtime'")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS severity TEXT NOT NULL DEFAULT 'warning'")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS fingerprint TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS details JSONB NOT NULL DEFAULT '{}'::jsonb")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS task_id TEXT")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'observer-worker'")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'open'")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolved_at TIMESTAMPTZ")
            await cur.execute("ALTER TABLE incidents ADD COLUMN IF NOT EXISTS resolution_reason TEXT")
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_incidents_tenant_status
                    ON incidents (tenant_id, status, occurred_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_incidents_fingerprint
                    ON incidents (fingerprint, occurred_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS readiness_reports (
                    readiness_report_id BIGSERIAL PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    health TEXT NOT NULL,
                    open_tasks INTEGER NOT NULL DEFAULT 0,
                    in_progress_tasks INTEGER NOT NULL DEFAULT 0,
                    failed_tasks INTEGER NOT NULL DEFAULT 0,
                    blocked_tasks INTEGER NOT NULL DEFAULT 0,
                    active_agents INTEGER NOT NULL DEFAULT 0,
                    incidents_open INTEGER NOT NULL DEFAULT 0,
                    report JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS tenant_id TEXT NOT NULL DEFAULT ''")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'unknown'")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS health TEXT NOT NULL DEFAULT 'unknown'")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS open_tasks INTEGER NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS in_progress_tasks INTEGER NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS failed_tasks INTEGER NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS blocked_tasks INTEGER NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS active_agents INTEGER NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS incidents_open INTEGER NOT NULL DEFAULT 0")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS report JSONB NOT NULL DEFAULT '{}'::jsonb")
            await cur.execute("ALTER TABLE readiness_reports ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()")
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    api_key TEXT UNIQUE NOT NULL,
                    plan TEXT DEFAULT 'free',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
                """
            )
            await cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT DEFAULT 'free'")
            await cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS requests_per_minute INTEGER DEFAULT 60")
            await cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tokens_per_day BIGINT DEFAULT 500000")
            await cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_url TEXT")
            await cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_secret TEXT")
            await cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb")
            await cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW()")

            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_readiness_reports_tenant_created
                    ON readiness_reports (tenant_id, created_at DESC)
                """
            )
        await conn.commit()


async def _try_tick_lock(cur, lock_name: str) -> bool:
    await cur.execute("SELECT pg_try_advisory_lock(hashtext(%s)) AS locked", (lock_name,))
    row = await cur.fetchone()
    return bool(row and row.get("locked"))


async def _unlock_tick(cur, lock_name: str) -> None:
    await cur.execute("SELECT pg_advisory_unlock(hashtext(%s))", (lock_name,))


async def _upsert_loop_state(
    *,
    tenant_id: str,
    project_id: str,
    cycle: int,
    phase: str,
    status: str,
    summary: str,
    metadata: dict[str, Any],
) -> None:
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO loop_states
                    (tenant_id, project_id, cycle, phase, status, summary, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (tenant_id, project_id) DO UPDATE SET
                    cycle = EXCLUDED.cycle,
                    phase = EXCLUDED.phase,
                    status = EXCLUDED.status,
                    summary = EXCLUDED.summary,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
                """,
                (tenant_id, project_id, cycle, phase, status, summary, json.dumps(metadata)),
            )
        await conn.commit()


async def compute_readiness_snapshot(tenant_id: str = "local") -> dict[str, Any]:
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            task_cols = await get_table_columns_cached(cur, "tasks")
            task_pk = "task_id" if "task_id" in task_cols else "id"
            task_scope = "WHERE tenant_id = %s" if "tenant_id" in task_cols else ""
            task_params: tuple[Any, ...] = (tenant_id,) if task_scope else ()
            await cur.execute(
                f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE status = 'pending') AS pending,
                    COUNT(*) FILTER (WHERE status = 'in-progress') AS in_progress,
                    COUNT(*) FILTER (WHERE status IN ('failed','needs-revision','dead-letter')) AS failed,
                    COUNT(*) FILTER (WHERE status LIKE 'blocked%%') AS blocked,
                    COUNT(*) FILTER (WHERE {task_pk} ILIKE 'REPAIR-%%' AND status NOT IN ('done','cancelled')) AS open_repairs
                FROM tasks
                {task_scope}
                """,
                task_params,
            )
            counts = await cur.fetchone() or {}

            heartbeat_cols = await get_table_columns_cached(cur, "heartbeats")
            heartbeat_scope = "WHERE tenant_id = %s" if "tenant_id" in heartbeat_cols else ""
            heartbeat_params: tuple[Any, ...] = (tenant_id,) if heartbeat_scope else ()
            await cur.execute(
                f"""
                SELECT
                    COUNT(DISTINCT agent_name) AS active_agents
                FROM heartbeats
                {heartbeat_scope}
                """
                if heartbeat_scope
                else """
                SELECT COUNT(DISTINCT agent_name) AS active_agents
                FROM heartbeats
                WHERE beat_at > NOW() - (%s * INTERVAL '1 minute')
                """,
                heartbeat_params if heartbeat_scope else (TASK_STALE_TIMEOUT_M,),
            )
            active_row = await cur.fetchone() or {}

            await cur.execute(
                """
                SELECT COUNT(*) AS open_incidents
                FROM incidents
                WHERE tenant_id = %s
                  AND status = 'open'
                  AND category <> 'runtime-readiness'
                """,
                (tenant_id,),
            )
            incident_row = await cur.fetchone() or {}

    total = int(counts.get("total") or 0)
    pending = int(counts.get("pending") or 0)
    in_progress = int(counts.get("in_progress") or 0)
    failed = int(counts.get("failed") or 0)
    blocked = int(counts.get("blocked") or 0)
    open_repairs = int(counts.get("open_repairs") or 0)
    active_agents = int(active_row.get("active_agents") or 0)
    open_incidents = int(incident_row.get("open_incidents") or 0)
    cognitive = await _get_cognitive_quality_snapshot()
    cognitive_status = str(cognitive.get("quality_status") or "unknown")
    quality = "full" if cognitive_status == "full" else "degraded"

    health = "ok"
    readiness = "ready"
    if failed > 0 or open_incidents > 0:
        health = "degraded"
        readiness = "not_ready"
    elif blocked > 0 or open_repairs > 0 or active_agents == 0:
        health = "needs-answers" if active_agents == 0 and total == 0 else "degraded"
        readiness = "degraded"
    elif cognitive_status in {"limited", "unavailable"}:
        health = "degraded"
        readiness = "degraded"

    return {
        "tenant_id": tenant_id,
        "status": readiness,
        "health": health,
        "quality": quality,
        "cognitive_status": cognitive_status,
        "counts": {
            "total": total,
            "pending": pending,
            "in_progress": in_progress,
            "failed": failed,
            "blocked": blocked,
            "open_repairs": open_repairs,
            "active_agents": active_agents,
            "open_incidents": open_incidents,
        },
        "cognitive": {
            "status": cognitive_status,
            "score": cognitive.get("score"),
            "critical_missing": cognitive.get("critical_missing") or [],
            "optional_missing": cognitive.get("optional_missing") or [],
            "summary": cognitive.get("summary") or "",
        },
        "ts": now_iso(),
        "source": "db",
    }


async def store_readiness_snapshot(snapshot: dict[str, Any]) -> None:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            counts = snapshot.get("counts", {})
            await cur.execute(
                """
                INSERT INTO readiness_reports
                    (tenant_id, status, health, open_tasks, in_progress_tasks, failed_tasks,
                     blocked_tasks, active_agents, incidents_open, report)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                """,
                (
                    snapshot.get("tenant_id", "local"),
                    snapshot.get("status", "unknown"),
                    snapshot.get("health", "unknown"),
                    counts.get("pending", 0),
                    counts.get("in_progress", 0),
                    counts.get("failed", 0),
                    counts.get("blocked", 0),
                    counts.get("active_agents", 0),
                    counts.get("open_incidents", 0),
                    json.dumps(snapshot),
                ),
            )
        await conn.commit()


async def get_latest_readiness_snapshot(tenant_id: str = "local") -> dict[str, Any]:
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT report
                FROM readiness_reports
                WHERE tenant_id = %s
                ORDER BY created_at DESC, readiness_report_id DESC
                LIMIT 1
                """,
                (tenant_id,),
            )
            row = await cur.fetchone()
    if row and row.get("report"):
        report = row["report"]
        if isinstance(report, str):
            try:
                report = json.loads(report)
            except json.JSONDecodeError:
                report = {}
        if isinstance(report, dict) and report:
            report.setdefault("source", "db")
            return report
    return await compute_readiness_snapshot(tenant_id)


async def list_incidents(
    tenant_id: str = "local",
    limit: int = 50,
    status_filter: str = "all",
) -> list[dict[str, Any]]:
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            where_parts = ["tenant_id = %s"]
            params: list[Any] = [tenant_id]
            if status_filter != "all":
                where_parts.append("status = %s")
                params.append(status_filter)
            params.append(limit)
            await cur.execute(
                """
                SELECT incident_id, tenant_id, project_id, category, severity, fingerprint,
                       summary, details, task_id, source, status, occurred_at, updated_at,
                       resolved_at, resolution_reason
                FROM incidents
                WHERE {where_sql}
                ORDER BY occurred_at DESC
                LIMIT %s
                """.format(where_sql=" AND ".join(where_parts)),
                tuple(params),
            )
            return [dict(row) for row in await cur.fetchall()]


async def _resolve_open_incidents(
    *,
    tenant_id: str,
    category: str,
    reason: str,
    project_id: str = "",
    fingerprint: str | None = None,
    exclude_fingerprint: str | None = None,
) -> int:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            where_parts = [
                "tenant_id = %s",
                "status = 'open'",
                "category = %s",
            ]
            params: list[Any] = [tenant_id, category]
            if project_id:
                where_parts.append("project_id = %s")
                params.append(project_id)
            if fingerprint is not None:
                where_parts.append("fingerprint = %s")
                params.append(fingerprint)
            if exclude_fingerprint is not None:
                where_parts.append("fingerprint <> %s")
                params.append(exclude_fingerprint)
            await cur.execute(
                f"""
                UPDATE incidents
                SET status = 'resolved',
                    updated_at = NOW(),
                    resolved_at = NOW(),
                    resolution_reason = %s
                WHERE {' AND '.join(where_parts)}
                """,
                tuple([reason] + params),
            )
            resolved = int(cur.rowcount or 0)
        await conn.commit()
    return resolved


async def _resolve_watchdog_stale_recovery_incidents(
    *,
    tenant_id: str,
    project_id: str = "",
) -> int:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            task_cols = await get_table_columns_cached(cur, "tasks")
            task_pk = await get_task_pk_column(cur)
            task_join = (
                f"SELECT 1 FROM tasks t WHERE t.{task_pk} = incidents.task_id AND t.tenant_id = incidents.tenant_id AND t.status = 'in-progress'"
                if "tenant_id" in task_cols
                else f"SELECT 1 FROM tasks t WHERE t.{task_pk} = incidents.task_id AND t.status = 'in-progress'"
            )
            where_parts = [
                "tenant_id = %s",
                "status = 'open'",
                "category = 'watchdog-stale-recovery'",
            ]
            params: list[Any] = [tenant_id]
            if project_id:
                where_parts.append("project_id = %s")
                params.append(project_id)
            await cur.execute(
                f"""
                UPDATE incidents
                SET status = 'resolved',
                    updated_at = NOW(),
                    resolved_at = NOW(),
                    resolution_reason = %s
                WHERE {' AND '.join(where_parts)}
                  AND (
                      task_id IS NULL
                      OR NOT EXISTS ({task_join})
                  )
                """,
                tuple(["task-no-longer-in-progress"] + params),
            )
            resolved = int(cur.rowcount or 0)
        await conn.commit()
    return resolved


def _runtime_readiness_fingerprint(readiness: dict[str, Any]) -> str:
    counts = readiness.get("counts", {})
    return (
        f"readiness:{readiness.get('tenant_id', 'local')}:"
        f"failed={counts.get('failed', 0)}:"
        f"blocked={counts.get('blocked', 0)}:"
        f"repairs={counts.get('open_repairs', 0)}:"
        f"incidents={counts.get('open_incidents', 0)}:"
        f"active_agents={counts.get('active_agents', 0)}"
    )


async def reconcile_incidents(
    *,
    tenant_id: str = "local",
    project_id: str = "",
    readiness: dict[str, Any] | None = None,
    stale_tasks: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    stale_tasks = stale_tasks or []
    readiness = readiness or await compute_readiness_snapshot(tenant_id)
    resolved = 0

    if stale_tasks:
        stale_fingerprint = f"stale-tasks:{tenant_id}:{len(stale_tasks)}"
        resolved += await _resolve_open_incidents(
            tenant_id=tenant_id,
            project_id=project_id,
            category="stale-tasks",
            exclude_fingerprint=stale_fingerprint,
            reason="superseded-by-current-stale-set",
        )
    else:
        resolved += await _resolve_open_incidents(
            tenant_id=tenant_id,
            project_id=project_id,
            category="stale-tasks",
            reason="stale-tasks-cleared",
        )

    resolved += await _resolve_watchdog_stale_recovery_incidents(
        tenant_id=tenant_id,
        project_id=project_id,
    )

    runtime_fp = _runtime_readiness_fingerprint(readiness)
    if readiness.get("status") == "not_ready":
        resolved += await _resolve_open_incidents(
            tenant_id=tenant_id,
            project_id=project_id,
            category="runtime-readiness",
            exclude_fingerprint=runtime_fp,
            reason="superseded-by-current-runtime-readiness",
        )
    else:
        resolved += await _resolve_open_incidents(
            tenant_id=tenant_id,
            project_id=project_id,
            category="runtime-readiness",
            reason="runtime-recovered",
        )

    return {"resolved": resolved}


async def _record_incident_if_needed(
    *,
    tenant_id: str,
    category: str,
    severity: str,
    fingerprint: str,
    summary: str,
    details: dict[str, Any],
    task_id: str | None = None,
    project_id: str = "",
    source: str = "observer-worker",
) -> bool:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT incident_id
                FROM incidents
                WHERE tenant_id = %s
                  AND fingerprint = %s
                  AND occurred_at > NOW() - (%s * INTERVAL '1 second')
                ORDER BY occurred_at DESC
                LIMIT 1
                """,
                (tenant_id, fingerprint, INCIDENT_COOLDOWN_S),
            )
            existing = await cur.fetchone()
            if existing:
                return False
            await cur.execute(
                """
                INSERT INTO incidents
                    (tenant_id, project_id, category, severity, fingerprint, summary, details, task_id, source, status)
                VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, 'open')
                """,
                (tenant_id, project_id, category, severity, fingerprint, summary, json.dumps(details), task_id, source),
            )
        await conn.commit()
    return True


async def ensure_repair_task(
    *,
    tenant_id: str,
    fingerprint: str,
    summary: str,
    details: dict[str, Any],
    source_task_id: str | None = None,
    project_id: str = "",
    priority: int = 1,
    assigned_agent: str | None = None,
) -> str | None:
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            task_cols = await get_table_columns_cached(cur, "tasks")
            if not task_cols:
                return None
            task_pk = await get_task_pk_column(cur)
            where_parts = [f"{task_pk} ILIKE 'REPAIR-%%'", "status NOT IN ('done', 'cancelled', 'dead-letter')"]
            params: list[Any] = []
            if "tenant_id" in task_cols:
                where_parts.insert(0, "tenant_id = %s")
                params.append(tenant_id)
            if "metadata" in task_cols:
                where_parts.append("metadata->>'repair_fingerprint' = %s")
                params.append(fingerprint)
            else:
                where_parts.append("title = %s")
                params.append(summary)
            await cur.execute(
                f"""
                SELECT {task_pk} AS task_id
                FROM tasks
                WHERE {' AND '.join(where_parts)}
                ORDER BY created_at DESC
                LIMIT 1
                """,
                tuple(params),
            )
            existing = await cur.fetchone()
            if existing:
                return str(existing["task_id"])

            repair_id = f"REPAIR-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()}"
            insert_cols = [task_pk, "title", "description", "status", "priority", "created_at", "updated_at"]
            insert_vals: list[Any] = [
                repair_id,
                summary[:255],
                json.dumps(details, ensure_ascii=False) if details else summary,
                "pending",
                priority,
            ]
            if "tenant_id" in task_cols:
                insert_cols.append("tenant_id")
                insert_vals.append(tenant_id)
            if "project_id" in task_cols:
                insert_cols.append("project_id")
                insert_vals.append(project_id)
            if "assigned_agent" in task_cols:
                insert_cols.append("assigned_agent")
                insert_vals.append(assigned_agent)
            if "metadata" in task_cols:
                insert_cols.append("metadata")
                insert_vals.append(
                    json.dumps(
                        {
                            "repair_fingerprint": fingerprint,
                            "repair_source_task_id": source_task_id,
                            "repair_details": details,
                        },
                        ensure_ascii=False,
                    )
                )

            value_parts: list[str] = []
            dynamic_index = 0
            for col in insert_cols:
                if col == "created_at":
                    value_parts.append("NOW()")
                elif col == "updated_at":
                    value_parts.append("NOW()")
                elif col == "metadata":
                    value_parts.append("%s::jsonb")
                    dynamic_index += 1
                else:
                    value_parts.append("%s")
                    dynamic_index += 1

            placeholders = ", ".join(value_parts)
            await cur.execute(
                f"INSERT INTO tasks ({', '.join(insert_cols)}) VALUES ({placeholders})",
                tuple(insert_vals),
            )
            await insert_agent_event(
                cur,
                task_id=repair_id,
                event_type="repair_seeded",
                payload={
                    "fingerprint": fingerprint,
                    "source_task_id": source_task_id,
                    "source": "runtime-plane",
                },
                agent_name="runtime-plane",
                tenant_id=tenant_id,
            )
        await conn.commit()
    return repair_id


async def readiness_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    readiness = await compute_readiness_snapshot(tenant_id)
    await store_readiness_snapshot(readiness)
    await _upsert_loop_state(
        tenant_id=tenant_id,
        project_id=project_id,
        cycle=int(datetime.now(timezone.utc).timestamp()),
        phase="readiness",
        status=readiness["status"],
        summary=f"health={readiness['health']}",
        metadata={"readiness": readiness},
    )
    return {"status": "ok", "tenant_id": tenant_id, "project_id": project_id, "readiness": readiness, "ts": now_iso()}


async def scheduler_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_runtime_plane_schema()
    promoted = 0
    dispatched = 0
    assigned = 0
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            if not await _try_tick_lock(cur, "streaming_scheduler_tick"):
                return {"status": "skipped", "reason": "lock-held"}

            try:
                task_cols = await get_table_columns_cached(cur, "tasks")
                task_pk = await get_task_pk_column(cur)
                dep_col = await get_dependency_ref_column(cur)
                task_scope = "t.tenant_id = %s AND " if "tenant_id" in task_cols else ""
                task_scope_params: tuple[Any, ...] = (tenant_id,) if "tenant_id" in task_cols else ()

                await cur.execute(
                    f"""
                    UPDATE tasks t
                    SET status = 'pending',
                        updated_at = NOW()
                    WHERE {task_scope}
                          t.status = 'blocked-deps'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM dependencies d
                          JOIN tasks dep ON dep.{task_pk} = d.{dep_col}
                          WHERE d.task_id = t.{task_pk}
                            AND dep.status NOT IN ('done', 'cancelled')
                      )
                    RETURNING t.{task_pk} AS task_id
                    """,
                    task_scope_params,
                )
                promoted_rows = await cur.fetchall()
                promoted = len(promoted_rows)

                dispatch_cols = await get_table_columns_cached(cur, "webhook_dispatches")
                dispatch_has_tenant = "tenant_id" in dispatch_cols
                rep_cols = await get_table_columns_cached(cur, "agent_reputation")
                rep_has_tenant = "tenant_id" in rep_cols

                await cur.execute(
                    f"""
                    SELECT t.{task_pk} AS task_id, t.title, t.description, t.priority, t.project_id, t.assigned_agent, t.metadata
                    FROM tasks t
                    WHERE {task_scope}
                          t.status = 'pending'
                      AND NOT EXISTS (
                          SELECT 1
                          FROM webhook_dispatches wd
                          WHERE wd.task_id = t.{task_pk}
                            AND wd.status IN ('pending', 'delivered')
                      )
                    ORDER BY t.priority ASC, t.created_at ASC
                    LIMIT 50
                    """,
                    task_scope_params,
                )
                ready_tasks = [dict(row) for row in await cur.fetchall()]

                rep_where = "WHERE tenant_id = %s" if rep_has_tenant else ""
                rep_params: tuple[Any, ...] = (tenant_id,) if rep_has_tenant else ()
                await cur.execute(
                    f"""
                    SELECT agent_name
                    FROM agent_reputation
                    {rep_where}
                    ORDER BY COALESCE(reputation_fit_score, 0.5) DESC, tasks_total DESC
                    LIMIT 20
                    """,
                    rep_params,
                )
                agent_rows = await cur.fetchall()
                available_agents = [str(row["agent_name"]) for row in agent_rows if row.get("agent_name")]

                if not available_agents:
                    available_agents = ["agent-worker"]

                for index, task in enumerate(ready_tasks):
                    metadata = task.get("metadata") or {}
                    if isinstance(metadata, str):
                        try:
                            metadata = json.loads(metadata)
                        except json.JSONDecodeError:
                            metadata = {}
                    preferred_agent = metadata.get("preferred_agent") if isinstance(metadata, dict) else None
                    agent_name = task.get("assigned_agent") or preferred_agent or available_agents[index % len(available_agents)]
                    await cur.execute(
                        f"UPDATE tasks SET assigned_agent = %s, updated_at = NOW() WHERE {task_pk} = %s",
                        (agent_name, task["task_id"]),
                    )
                    payload = {
                        "id": task["task_id"],
                        "task_id": task["task_id"],
                        "title": task.get("title") or "",
                        "description": task.get("description") or "",
                        "priority": task.get("priority"),
                        "project_id": task.get("project_id") or "",
                        "assigned_agent": agent_name,
                        "metadata": metadata if isinstance(metadata, dict) else {},
                    }
                    insert_cols = ["task_id", "agent_name", "status", "dispatch_payload", "dispatched_at"]
                    insert_vals: list[Any] = [task["task_id"], agent_name, "pending", json.dumps(payload), datetime.now(timezone.utc)]
                    if dispatch_has_tenant:
                        insert_cols.append("tenant_id")
                        insert_vals.append(tenant_id)
                    placeholders = ", ".join(["%s"] * len(insert_cols))
                    await cur.execute(
                        f"""
                        INSERT INTO webhook_dispatches ({', '.join(insert_cols)})
                        VALUES ({placeholders})
                        """,
                        tuple(insert_vals),
                    )
                    assigned += 1
                    dispatched += 1
                    await insert_agent_event(
                        cur,
                        task_id=task["task_id"],
                        event_type="dispatch",
                        payload={"agent": agent_name, "source": "scheduler-worker"},
                        agent_name="scheduler-worker",
                        tenant_id=tenant_id,
                    )

                await conn.commit()
                return {
                    "status": "ok",
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "promoted": promoted,
                    "assigned": assigned,
                    "dispatched": dispatched,
                    "ts": now_iso(),
                }
            finally:
                await _unlock_tick(cur, "streaming_scheduler_tick")


async def observer_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            if not await _try_tick_lock(cur, "streaming_observer_tick"):
                return {"status": "skipped", "reason": "lock-held"}
            try:
                task_cols = await get_table_columns_cached(cur, "tasks")
                task_pk = await get_task_pk_column(cur)
                hb_cols = await get_table_columns_cached(cur, "heartbeats")
                task_scope = "t.tenant_id = %s AND " if "tenant_id" in task_cols else ""
                task_params: tuple[Any, ...] = (tenant_id,) if "tenant_id" in task_cols else ()
                heartbeat_ts_col = "beat_at" if "beat_at" in hb_cols else ("updated_at" if "updated_at" in hb_cols else "")
                heartbeat_join = (
                    f"LEFT JOIN heartbeats h ON h.task_id = t.{task_pk} AND h.agent_name = t.assigned_agent AND h.tenant_id = t.tenant_id"
                    if "tenant_id" in hb_cols and "tenant_id" in task_cols
                    else f"LEFT JOIN heartbeats h ON h.task_id = t.{task_pk} AND h.agent_name = t.assigned_agent"
                )
                stale_clause = (
                    f"(h.{heartbeat_ts_col} IS NULL OR h.{heartbeat_ts_col} < NOW() - (%s * INTERVAL '1 minute'))"
                    if heartbeat_ts_col
                    else "t.updated_at < NOW() - (%s * INTERVAL '1 minute')"
                )
                select_hb = f"h.{heartbeat_ts_col} AS beat_at" if heartbeat_ts_col else "NULL::timestamptz AS beat_at"
                await cur.execute(
                    f"""
                    SELECT t.{task_pk} AS task_id, t.assigned_agent, t.status, t.updated_at, {select_hb}
                    FROM tasks t
                    {heartbeat_join}
                    WHERE {task_scope}
                          t.status = 'in-progress'
                      AND {stale_clause}
                    ORDER BY t.updated_at ASC
                    LIMIT 25
                    """,
                    task_params + (TASK_STALE_TIMEOUT_M,),
                )
                stale_tasks = [dict(row) for row in await cur.fetchall()]

                readiness = await compute_readiness_snapshot(tenant_id=tenant_id)
                reconciled = await reconcile_incidents(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    readiness=readiness,
                    stale_tasks=stale_tasks,
                )
                readiness = await compute_readiness_snapshot(tenant_id=tenant_id)
                await store_readiness_snapshot(readiness)

                incidents_created = 0
                if stale_tasks:
                    fingerprint = f"stale-tasks:{tenant_id}:{len(stale_tasks)}"
                    created = await _record_incident_if_needed(
                        tenant_id=tenant_id,
                        category="stale-tasks",
                        severity="warning",
                        fingerprint=fingerprint,
                        summary=f"{len(stale_tasks)} in-progress tasks without recent heartbeat",
                        details={"tasks": stale_tasks},
                        project_id=project_id,
                    )
                    incidents_created += int(created)
                if readiness["status"] == "not_ready":
                    fingerprint = _runtime_readiness_fingerprint(readiness)
                    created = await _record_incident_if_needed(
                        tenant_id=tenant_id,
                        category="runtime-readiness",
                        severity="critical",
                        fingerprint=fingerprint,
                        summary="Runtime is not ready",
                        details=readiness,
                        project_id=project_id,
                    )
                    incidents_created += int(created)

                await _upsert_loop_state(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    cycle=int(datetime.now(timezone.utc).timestamp()),
                    phase="observer",
                    status=readiness["status"],
                    summary=f"health={readiness['health']} incidents_created={incidents_created}",
                    metadata={
                        "stale_tasks": len(stale_tasks),
                        "readiness": readiness,
                        "incidents_created": incidents_created,
                        "incidents_resolved": reconciled["resolved"],
                    },
                )
                return {
                    "status": "ok",
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "stale_tasks": len(stale_tasks),
                    "incidents_created": incidents_created,
                    "incidents_resolved": reconciled["resolved"],
                    "readiness": readiness,
                    "ts": now_iso(),
                }
            finally:
                await _unlock_tick(cur, "streaming_observer_tick")


async def run_scheduler_loop(tenant_id: str = "local", project_id: str = "") -> None:
    log.info("scheduler_worker_started tenant=%s interval=%s", tenant_id, SCHEDULER_INTERVAL_S)
    while True:
        try:
            await scheduler_tick_once(tenant_id=tenant_id, project_id=project_id)
        except Exception as exc:
            log.exception("scheduler_worker_error error=%s", exc)
        await asyncio.sleep(SCHEDULER_INTERVAL_S)


async def run_observer_loop(tenant_id: str = "local", project_id: str = "") -> None:
    log.info("observer_worker_started tenant=%s interval=%s", tenant_id, OBSERVER_INTERVAL_S)
    while True:
        try:
            await observer_tick_once(tenant_id=tenant_id, project_id=project_id)
        except Exception as exc:
            log.exception("observer_worker_error error=%s", exc)
        await asyncio.sleep(OBSERVER_INTERVAL_S)


async def run_readiness_loop(tenant_id: str = "local", project_id: str = "") -> None:
    log.info("readiness_worker_started tenant=%s interval=%s", tenant_id, OBSERVER_INTERVAL_S)
    while True:
        try:
            await readiness_tick_once(tenant_id=tenant_id, project_id=project_id)
        except Exception as exc:
            log.exception("readiness_worker_error error=%s", exc)
        await asyncio.sleep(OBSERVER_INTERVAL_S)
