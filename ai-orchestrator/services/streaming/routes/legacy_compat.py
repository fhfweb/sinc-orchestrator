"""
Legacy compatibility router.

This keeps old dashboard/control clients alive while the canonical FastAPI
surface replaces the historical Flask server.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from services.streaming.core.auth import get_tenant_id
from services.streaming.core.db import async_db
from services.streaming.core.redis_ import get_async_redis
from services.streaming.core.schema_compat import get_table_columns_cached
from services.streaming.routes.admin import system_control

router = APIRouter(tags=["legacy_compat"])


class LegacyCommandBody(BaseModel):
    command: str = Field(..., min_length=1)


class ConfidenceBody(BaseModel):
    value: float = Field(..., ge=0.0, le=100.0)


class SystemModeBody(BaseModel):
    mode: str = Field(..., min_length=1)


async def _set_runtime_config(tenant_id: str, key: str, value: Any) -> None:
    redis_client = get_async_redis()
    if redis_client:
        await redis_client.set(f"sinc:config:{tenant_id}:{key}", value)


async def _get_runtime_config(tenant_id: str, key: str, default: Any = None) -> Any:
    redis_client = get_async_redis()
    if redis_client:
        val = await redis_client.get(f"sinc:config:{tenant_id}:{key}")
        return val if val is not None else default
    return default


def _parse_legacy_command(command: str) -> dict[str, Any]:
    raw = str(command or "").strip()
    cmd = raw.lower().lstrip("/")
    parts = cmd.split()
    if not parts:
        raise HTTPException(status_code=400, detail="command required")

    if parts[:2] == ["kill", "agent"] and len(parts) >= 3:
        return {"command": "kill-agent", "data": {"name": raw.split(None, 2)[2]}}
    if parts[:2] == ["stop", "agent"] and len(parts) >= 3:
        return {"command": "stop-agent", "data": {"name": raw.split(None, 2)[2]}}
    if parts[:2] == ["reclaim", "task"] and len(parts) >= 3:
        return {"command": "reclaim-task", "data": {"id": raw.split(None, 2)[2]}}
    if parts[:2] == ["list", "zombies"]:
        return {"command": "list-zombies", "data": {}}
    if parts[:2] == ["snapshot", "stack"]:
        return {"command": "snapshot-stack", "data": {}}
    if parts[:2] == ["inspect", "prompt"] and len(parts) >= 3:
        return {"command": "inspect-prompt", "data": {"id": raw.split(None, 2)[2]}}
    if parts[:2] == ["tenant", "quota"] and len(parts) >= 4:
        return {
            "command": "tenant-quota",
            "data": {"name": parts[2], "tokens": parts[3]},
        }
    if parts[:2] == ["set", "confidence"] and len(parts) >= 3:
        try:
            value = float(parts[2])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid confidence value") from exc
        return {"command": "__set_confidence__", "data": {"value": value}}
    if parts[:2] == ["reset", "confidence"]:
        return {"command": "__set_confidence__", "data": {"value": 72.0}}

    return {"command": cmd, "data": {}}


async def _build_legacy_dashboard_state(tenant_id: str) -> dict[str, Any]:
    summary = {
        "projects": 1,
        "in_progress": 0,
        "pending": 0,
        "blocked": 0,
        "done": 0,
    }
    slo_metrics: dict[str, Any] = {}

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_cols = await get_table_columns_cached(cur, "tasks")
            if task_cols:
                has_tenant = "tenant_id" in task_cols
                await cur.execute(
                    """
                    SELECT status, COUNT(*) AS n
                      FROM tasks
                     {scope}
                     GROUP BY status
                    """.format(scope="WHERE tenant_id = %s" if has_tenant else ""),
                    (tenant_id,) if has_tenant else (),
                )
                rows = await cur.fetchall()
                for row in rows:
                    status = str(row["status"] or "").lower()
                    count = int(row["n"] or 0)
                    if status == "pending":
                        summary["pending"] += count
                    elif "block" in status:
                        summary["blocked"] += count
                    elif status in {"done", "completed"}:
                        summary["done"] += count
                    elif status in {"in-progress", "active", "delivered"}:
                        summary["in_progress"] += count

                await cur.execute(
                    """
                    SELECT percentile_cont(0.95) WITHIN GROUP (
                               ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000
                           ) AS cycle_p95_ms
                      FROM tasks
                     WHERE started_at IS NOT NULL
                       AND completed_at IS NOT NULL
                       {scope}
                    """.format(scope="AND tenant_id = %s" if has_tenant else ""),
                    (tenant_id,) if has_tenant else (),
                )
                row = await cur.fetchone()
                if row and row.get("cycle_p95_ms") is not None:
                    slo_metrics["cycle_p95_ms"] = round(float(row["cycle_p95_ms"]), 1)

    return {"dashboard": {"summary": summary, "slo": {"metrics": slo_metrics}}}


@router.get("/dashboard/state")
async def legacy_dashboard_state(tenant_id: str = Depends(get_tenant_id)):
    return await _build_legacy_dashboard_state(tenant_id)


@router.post("/api/command")
async def legacy_command_api(
    body: LegacyCommandBody,
    tenant_id: str = Depends(get_tenant_id),
):
    parsed = _parse_legacy_command(body.command)
    if parsed["command"] == "__set_confidence__":
        value = float(parsed["data"]["value"])
        await _set_runtime_config(tenant_id, "confidence_threshold", value)
        return {"success": True, "confidence": value}
    result = await system_control(parsed, tenant_id=tenant_id)
    return {"success": bool(result.get("ok")), **result}


@router.get("/api/config/confidence")
async def get_legacy_confidence_api(
    tenant_id: str = Depends(get_tenant_id),
):
    val = await _get_runtime_config(tenant_id, "confidence_threshold", 72.0)
    return {"success": True, "confidence": float(val)}


@router.post("/api/config/confidence")
async def legacy_confidence_api(
    body: ConfidenceBody,
    tenant_id: str = Depends(get_tenant_id),
):
    await _set_runtime_config(tenant_id, "confidence_threshold", body.value)
    return {"success": True, "confidence": body.value}


@router.get("/api/system/mode")
async def get_legacy_system_mode_api(
    tenant_id: str = Depends(get_tenant_id),
):
    val = await _get_runtime_config(tenant_id, "system_mode", "normal")
    return {"success": True, "mode": str(val)}


@router.post("/api/system/mode")
async def legacy_system_mode_api(
    body: SystemModeBody,
    tenant_id: str = Depends(get_tenant_id),
):
    mode = body.mode.strip().lower()
    if mode not in {"normal", "safe", "kill", "maintenance"}:
        raise HTTPException(status_code=400, detail="invalid mode")
    await _set_runtime_config(tenant_id, "system_mode", mode)
    return {"success": True, "mode": mode}
