"""
streaming/routes/gates.py
=========================
FastAPI Router for Human Approval Gates.
"""
import logging
import asyncio
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db
from services.streaming.core.schema_compat import get_task_pk_column, get_table_columns_cached
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["gates"])


async def _table_has_tenant(cur, table_name: str) -> bool:
    return "tenant_id" in await get_table_columns_cached(cur, table_name)

# ── Models ───────────────────────────────────────────────────────────────────

class GateDecisionRequest(BaseModel):
    decided_by: str = "human"
    reason: str = ""

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/gates")
async def list_gates(tenant_id: str = Depends(get_tenant_id)):
    """List all pending human approval gates for the authenticated tenant."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_pk = await get_task_pk_column(cur)
            tasks_has_tenant = await _table_has_tenant(cur, "tasks")
            await cur.execute(
                """
                SELECT g.* FROM human_gates g
                JOIN tasks t ON t.{task_pk} = g.task_id
                {task_scope}
                ORDER BY g.requested_at DESC LIMIT 100
                """.format(
                    task_pk=task_pk,
                    task_scope="WHERE t.tenant_id = %s" if tasks_has_tenant else "",
                ),
                (tenant_id,) if tasks_has_tenant else (),
            )
            rows = await cur.fetchall()
    return {"gates": rows, "total": len(rows)}

@router.post("/gates/{gate_id}/approve")
async def approve_gate(
    gate_id: int,
    body: GateDecisionRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Approve a human gate. Unblocks the associated task."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                tasks_has_tenant = await _table_has_tenant(cur, "tasks")
                await cur.execute(
                    """
                    UPDATE human_gates hg
                    SET status = 'approved', decided_at = NOW(),
                        decided_by = %s, reason = %s
                    FROM tasks t
                    WHERE hg.id = %s AND hg.status = 'pending'
                      AND hg.task_id = t.{task_pk}
                      {task_scope}
                    RETURNING hg.task_id
                    """.format(
                        task_pk=task_pk,
                        task_scope="AND t.tenant_id = %s" if tasks_has_tenant else "",
                    ),
                    (body.decided_by, body.reason, gate_id, tenant_id)
                    if tasks_has_tenant
                    else (body.decided_by, body.reason, gate_id),
                )
                row = await cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="gate not found or already decided")

                task_id = row["task_id"]
                await cur.execute(
                    """
                    UPDATE tasks SET status = 'pending', updated_at = NOW()
                    WHERE {task_pk} = %s AND status IN ('blocked-phase-approval', 'blocked-gate')
                    """.format(task_pk=task_pk),
                    (task_id,),
                )
                await conn.commit()

        await broadcast("gate_approved", {
            "gate_id":    gate_id,
            "task_id":    task_id,
            "decided_by": body.decided_by,
            "reason":     body.reason,
        }, tenant_id=tenant_id)

        return {"ok": True, "gate_id": gate_id, "task_id": task_id}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"approve_gate_error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/gates/{gate_id}/reject")
async def reject_gate(
    gate_id: int,
    body: GateDecisionRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Reject a human gate."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                tasks_has_tenant = await _table_has_tenant(cur, "tasks")
                await cur.execute(
                    """
                    UPDATE human_gates hg
                    SET status = 'rejected', decided_at = NOW(),
                        decided_by = %s, reason = %s
                    FROM tasks t
                    WHERE hg.id = %s AND hg.status = 'pending'
                      AND hg.task_id = t.{task_pk}
                      {task_scope}
                    RETURNING hg.task_id
                    """.format(
                        task_pk=task_pk,
                        task_scope="AND t.tenant_id = %s" if tasks_has_tenant else "",
                    ),
                    (body.decided_by, body.reason, gate_id, tenant_id)
                    if tasks_has_tenant
                    else (body.decided_by, body.reason, gate_id),
                )
                row = await cur.fetchone()
                if not row:
                    raise HTTPException(status_code=404, detail="gate not found or already decided")
                await conn.commit()

        await broadcast("gate_rejected", {
            "gate_id":    gate_id,
            "task_id":    row["task_id"],
            "decided_by": body.decided_by,
        }, tenant_id=tenant_id)

        return {"ok": True, "gate_id": gate_id}
    except HTTPException:
        raise
    except Exception as e:
        log.error(f"reject_gate_error: {e}")
        raise HTTPException(status_code=500, detail=str(e))
