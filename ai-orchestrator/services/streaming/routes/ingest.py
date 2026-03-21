"""
streaming/routes/ingest.py
==========================
FastAPI Router for Ingestion operations.
"""
import logging
import os
import asyncio
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, BackgroundTasks
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant, enqueue_webhook
from services.streaming.core.db import async_db
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["ingest"])

# ── Models ───────────────────────────────────────────────────────────────────

class IngestRequest(BaseModel):
    project_path: str = ""
    project_id: str = ""
    repo_url: str = ""
    branch: str = ""
    deep: bool = False

# ── Background Task ──────────────────────────────────────────────────────────

async def _run_ingest_pipeline(pipeline_id: str, project_id: str, tenant_id: str, body: IngestRequest):
    """Execution logic for the ingestion pipeline."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE ingest_pipelines SET status='running', started_at=NOW(), updated_at=NOW() WHERE id=%s",
                    (pipeline_id,),
                )
                await conn.commit()
    except Exception:
        log.warning("ingest_pipeline_status_error", exc_info=True)

    await broadcast("ingest_started", {
        "pipeline_id": pipeline_id, "project_id": project_id, "deep": body.deep,
    }, tenant_id=tenant_id)

    error_msg = None
    files_indexed = nodes_created = edges_created = 0
    final_status  = "error"
    
    try:
        def _sync_run():
            from services.ingest_pipeline import IngestPipeline
            pipeline = IngestPipeline()
            return pipeline.run(
                pipeline_id=pipeline_id,
                project_path=body.project_path,
                project_id=project_id,
                tenant_id=tenant_id,
                deep=body.deep,
                repo_url=body.repo_url,
                branch=body.branch,
            )
        
        result = await asyncio.to_thread(_sync_run)
        files_indexed = result.get("files", 0)
        ast_stats     = result.get("ast", {})
        nodes_created = ast_stats.get("nodes_created", 0)
        edges_created = ast_stats.get("edges_created", 0)
        final_status  = "done"
    except Exception as exc:
        error_msg    = str(exc)
        final_status = "error"
        log.warning("ingest_pipeline_run_error error=%s", exc)

    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE ingest_pipelines
                    SET status=%s, completed_at=NOW(), finished_at=NOW(), updated_at=NOW(),
                        files_indexed=%s, nodes_created=%s, edges_created=%s, error=%s
                    WHERE id=%s
                    """,
                    (final_status, files_indexed, nodes_created,
                     edges_created, error_msg, pipeline_id),
                )
                await conn.commit()
    except Exception:
        log.warning("ingest_pipeline_final_error", exc_info=True)

    await broadcast(
        "ingest_done" if final_status == "done" else "ingest_error",
        {"pipeline_id": pipeline_id, "project_id": project_id,
         "files_indexed": files_indexed, "error": error_msg},
        tenant_id=tenant_id,
    )
    
    # Webhook (sync for now, or port to async if needed)
    enqueue_webhook(tenant_id, "ingest.done", {
        "pipeline_id": pipeline_id, "project_id": project_id,
        "status": final_status, "files_indexed": files_indexed,
    })

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(
    body: IngestRequest,
    backgroup_tasks: BackgroundTasks,
    tenant_id: str = Depends(get_tenant_id)
):
    """Trigger an async ingestion pipeline."""
    pipeline_id = f"INGEST-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()}"
    
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO ingest_pipelines
                        (id, project_id, tenant_id, project_path, deep, status, requested_at)
                    VALUES (%s, %s, %s, %s, %s, 'queued', NOW())
                    """,
                    (pipeline_id, body.project_id, tenant_id,
                     body.project_path or body.repo_url, body.deep),
                )
                await conn.commit()
    except Exception:
        log.warning("ingest_pipeline_insert_error", exc_info=True)

    # Emit to Redis Stream for the dedicated worker to pick up
    from services.event_bus import get_event_bus
    bus = await get_event_bus()
    await bus.emit("ingest", {
        "pipeline_id": pipeline_id,
        "project_id": body.project_id,
        "tenant_id": tenant_id,
        "project_path": body.project_path,
        "repo_url": body.repo_url,
        "branch": body.branch,
        "deep": body.deep
    })
    
    return {"ok": True, "pipeline_id": pipeline_id, "status": "queued"}

@router.get("/ingest/{pipeline_id}")
async def get_ingest_status(pipeline_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Get status and stats for a specific ingest pipeline run."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT * FROM ingest_pipelines WHERE id = %s AND tenant_id = %s",
                (pipeline_id, tenant_id),
            )
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="pipeline not found")
    return row

@router.get("/ingest")
async def list_ingests(
    project_id: Optional[str] = None,
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_tenant_id)
):
    """List recent ingest pipelines for the tenant."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            if project_id:
                await cur.execute(
                    "SELECT * FROM ingest_pipelines "
                    "WHERE tenant_id=%s AND project_id=%s "
                    "ORDER BY requested_at DESC LIMIT %s",
                    (tenant_id, project_id, limit),
                )
            else:
                await cur.execute(
                    "SELECT * FROM ingest_pipelines WHERE tenant_id=%s "
                    "ORDER BY requested_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
            rows = await cur.fetchall()
    return {"pipelines": rows, "total": len(rows)}
