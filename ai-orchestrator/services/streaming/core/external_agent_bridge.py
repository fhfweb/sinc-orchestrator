from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auth import now_iso
from .config import COMPLETIONS, DISPATCHES
from .db import async_db
from .runtime_plane import _try_tick_lock, _unlock_tick, ensure_runtime_plane_schema
from .schema_compat import get_table_columns_cached, get_task_pk_column, insert_agent_event
from services.streaming.routes.agents import (
    Completion,
    _apply_agent_completion,
    _publish_completion_audit,
    _sync_digital_twin,
)

log = logging.getLogger("orchestrator.external-bridge")

BRIDGE_INTERVAL_S = int(env_get("ORCHESTRATOR_EXTERNAL_BRIDGE_INTERVAL_SECONDS", default="20"))
BRIDGE_BATCH_SIZE = int(env_get("ORCHESTRATOR_EXTERNAL_BRIDGE_BATCH_SIZE", default="25"))
PROCESSED_COMPLETIONS = COMPLETIONS / "processed"
IN_PROGRESS_DISPATCHES = DISPATCHES / "in-progress"


def _ensure_bridge_dirs() -> None:
    DISPATCHES.mkdir(parents=True, exist_ok=True)
    IN_PROGRESS_DISPATCHES.mkdir(parents=True, exist_ok=True)
    COMPLETIONS.mkdir(parents=True, exist_ok=True)
    PROCESSED_COMPLETIONS.mkdir(parents=True, exist_ok=True)


def _parse_jsonish(value: Any) -> dict[str, Any]:
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


def _is_external_metadata(metadata: dict[str, Any]) -> bool:
    execution_mode = str(metadata.get("execution_mode") or "").strip().lower()
    return bool(metadata.get("external_bridge_enabled")) or execution_mode in {"external-agent", "manual", "human"}


def _dispatch_path(task_id: str) -> Path:
    return DISPATCHES / f"{task_id}.json"


def _in_progress_dispatch_path(task_id: str) -> Path:
    return IN_PROGRESS_DISPATCHES / f"{task_id}.json"


def _completion_archive_candidates(task_id: str) -> list[Path]:
    candidates = []
    simple = COMPLETIONS / f"{task_id}.json"
    if simple.exists():
        candidates.append(simple)
    candidates.extend(sorted(COMPLETIONS.glob(f"{task_id}-*.json")))
    return candidates


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temp_path.replace(path)


async def _list_external_dispatch_candidates(cur, *, tenant_id: str, project_id: str, limit: int) -> list[dict[str, Any]]:
    task_pk = await get_task_pk_column(cur)
    task_cols = await get_table_columns_cached(cur, "tasks")
    dispatch_cols = await get_table_columns_cached(cur, "webhook_dispatches")
    if "metadata" not in task_cols or not dispatch_cols:
        return []

    task_scope = []
    params: list[Any] = []
    if "tenant_id" in task_cols:
        task_scope.append("t.tenant_id = %s")
        params.append(tenant_id)
    if project_id and "project_id" in task_cols:
        task_scope.append("t.project_id = %s")
        params.append(project_id)

    dispatch_scope = ""
    if "tenant_id" in dispatch_cols:
        dispatch_scope = "AND wd.tenant_id = %s"
        params.append(tenant_id)

    task_scope_sql = (" AND ".join(task_scope) + " AND ") if task_scope else ""
    params.append(limit)

    await cur.execute(
        f"""
        SELECT
            wd.id AS dispatch_id,
            wd.status AS dispatch_status,
            wd.agent_name AS dispatch_agent,
            wd.dispatched_at,
            t.{task_pk} AS task_id,
            t.title,
            t.description,
            t.priority,
            t.assigned_agent,
            t.project_id,
            t.metadata
        FROM webhook_dispatches wd
        JOIN tasks t ON t.{task_pk} = wd.task_id
        WHERE {task_scope_sql}
              wd.status IN ('pending', 'delivered')
          {dispatch_scope}
          AND t.status = 'pending'
          AND (
                COALESCE(t.metadata->>'external_bridge_enabled', 'false') = 'true'
                OR LOWER(COALESCE(t.metadata->>'execution_mode', '')) IN ('external-agent', 'manual', 'human')
          )
        ORDER BY wd.dispatched_at ASC NULLS FIRST, wd.id ASC
        LIMIT %s
        """,
        tuple(params),
    )
    return [dict(row) for row in await cur.fetchall()]


def _build_dispatch_payload(row: dict[str, Any], *, tenant_id: str) -> dict[str, Any]:
    metadata = _parse_jsonish(row.get("metadata"))
    return {
        "schema_version": "v3",
        "bridge_version": "python-v1",
        "task_id": row["task_id"],
        "title": row.get("title") or "",
        "description": row.get("description") or "",
        "priority": row.get("priority"),
        "execution_mode": metadata.get("execution_mode", "external-agent"),
        "runtime_engine": metadata.get("runtime_engine", ""),
        "preferred_agent": metadata.get("preferred_agent", ""),
        "assigned_agent": row.get("assigned_agent") or row.get("dispatch_agent") or metadata.get("preferred_agent") or "",
        "requested_by_agent": metadata.get("requested_by_agent", "scheduler-worker"),
        "requested_at": now_iso(),
        "files_affected": list(metadata.get("files_affected") or []),
        "dependencies": list(metadata.get("dependencies") or []),
        "preflight_path": metadata.get("preflight_path", ""),
        "project_id": row.get("project_id") or "",
        "tenant_id": tenant_id,
        "dispatch_id": row.get("dispatch_id"),
        "metadata": metadata,
    }


async def _materialize_dispatches(*, tenant_id: str, project_id: str = "", limit: int = BRIDGE_BATCH_SIZE) -> int:
    dispatched = 0
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            rows = await _list_external_dispatch_candidates(
                cur,
                tenant_id=tenant_id,
                project_id=project_id,
                limit=limit,
            )
            if not rows:
                return 0

            dispatch_has_tenant = "tenant_id" in await get_table_columns_cached(cur, "webhook_dispatches")
            for row in rows:
                task_id = str(row["task_id"])
                if (COMPLETIONS / f"{task_id}.json").exists():
                    continue
                path = _dispatch_path(task_id)
                payload = _build_dispatch_payload(row, tenant_id=tenant_id)
                if not path.exists():
                    _write_json_atomic(path, payload)
                update_params: list[Any] = [json.dumps(payload), row["dispatch_id"]]
                tenant_clause = ""
                if dispatch_has_tenant:
                    tenant_clause = "AND tenant_id = %s"
                    update_params.append(tenant_id)
                await cur.execute(
                    f"""
                    UPDATE webhook_dispatches
                    SET status = 'delivered',
                        delivered_at = NOW(),
                        dispatch_payload = %s
                    WHERE id = %s
                      {tenant_clause}
                    """,
                    tuple(update_params),
                )
                await insert_agent_event(
                    cur,
                    task_id=task_id,
                    event_type="external_bridge_dispatch",
                    tenant_id=tenant_id,
                    agent_name="external-agent-bridge",
                    payload={
                        "dispatch_id": row.get("dispatch_id"),
                        "assigned_agent": payload.get("assigned_agent"),
                        "execution_mode": payload.get("execution_mode"),
                    },
                )
                dispatched += 1
        await conn.commit()
    return dispatched


async def _process_completion_artifact(path: Path, *, tenant_id: str) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("external_bridge_completion_parse_error file=%s error=%s", path.name, exc)
        return False

    task_id = str(payload.get("task_id") or "").strip()
    if not task_id or path.stem != task_id:
        return False

    try:
        completion = Completion(
            task_id=task_id,
            status=str(payload.get("status") or "failed"),
            summary=str(payload.get("summary") or ""),
            files_modified=list(payload.get("files_modified") or []),
            tests_passed=bool(payload.get("tests_passed", True)),
            policy_violations=list(payload.get("policy_violations") or []),
            next_suggested_tasks=list(payload.get("next_suggested_tasks") or []),
            backend_used=str(payload.get("backend_used") or "unknown"),
        )
    except Exception as exc:
        log.warning("external_bridge_completion_contract_error file=%s error=%s", path.name, exc)
        return False

    agent_name = str(payload.get("agent_name") or payload.get("executed_by") or "external-agent")
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            result = await _apply_agent_completion(
                conn,
                cur,
                agent_name=agent_name,
                tenant_id=tenant_id,
                body=completion,
            )
            await conn.commit()

    audit_event = result.get("audit_event")
    if audit_event:
        await _publish_completion_audit(audit_event)

    if result["files_modified"]:
        await _sync_digital_twin(task_id, result["files_modified"], tenant_id, result["project_id"])

    for candidate in _completion_archive_candidates(task_id):
        target = PROCESSED_COMPLETIONS / candidate.name
        try:
            candidate.replace(target)
        except FileNotFoundError:
            continue
    _dispatch_path(task_id).unlink(missing_ok=True)
    _in_progress_dispatch_path(task_id).unlink(missing_ok=True)
    return True


async def _drain_completion_artifacts(*, tenant_id: str, limit: int = BRIDGE_BATCH_SIZE) -> int:
    processed = 0
    for path in sorted(COMPLETIONS.glob("*.json"))[:limit]:
        if await _process_completion_artifact(path, tenant_id=tenant_id):
            processed += 1
    return processed


async def get_external_bridge_status(tenant_id: str = "local") -> dict[str, Any]:
    _ensure_bridge_dirs()
    pending_dispatch_files = len(list(DISPATCHES.glob("*.json")))
    in_progress_dispatch_files = len(list(IN_PROGRESS_DISPATCHES.glob("*.json")))
    pending_completion_files = 0
    if COMPLETIONS.exists():
        for path in COMPLETIONS.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if path.stem == str(payload.get("task_id") or ""):
                    pending_completion_files += 1
            except Exception:
                continue
    return {
        "tenant_id": tenant_id,
        "dispatch_files": pending_dispatch_files,
        "in_progress_dispatch_files": in_progress_dispatch_files,
        "completion_files": pending_completion_files,
        "processed_dir": str(PROCESSED_COMPLETIONS),
        "source": "filesystem+db",
        "ts": now_iso(),
    }


async def external_bridge_tick_once(tenant_id: str = "local", project_id: str = "") -> dict[str, Any]:
    _ensure_bridge_dirs()
    await ensure_runtime_plane_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            if not await _try_tick_lock(cur, "streaming_external_agent_bridge_tick"):
                return {"status": "skipped", "reason": "lock-held", "tenant_id": tenant_id}
            try:
                completions_processed = await _drain_completion_artifacts(tenant_id=tenant_id)
                dispatched = await _materialize_dispatches(tenant_id=tenant_id, project_id=project_id)
                return {
                    "status": "ok",
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "completions_processed": completions_processed,
                    "dispatched": dispatched,
                    "ts": now_iso(),
                }
            finally:
                await _unlock_tick(cur, "streaming_external_agent_bridge_tick")


async def run_external_bridge_loop(tenant_id: str = "local", project_id: str = "") -> None:
    _ensure_bridge_dirs()
    log.info("external_agent_bridge_started tenant=%s interval=%s", tenant_id, BRIDGE_INTERVAL_S)
    while True:
        try:
            await external_bridge_tick_once(tenant_id=tenant_id, project_id=project_id)
        except Exception as exc:
            log.exception("external_agent_bridge_error error=%s", exc)
        await asyncio.sleep(BRIDGE_INTERVAL_S)
