"""
streaming/routes/twin.py
========================
FastAPI Router for Digital Twin operations.
"""
import logging
import asyncio
from typing import Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id
from services.streaming.core.circuit import circuit_breaker
from services.streaming.core.db import async_db
from services.streaming.core.schema_compat import get_task_pk_column

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["twin"])

# ── Models ───────────────────────────────────────────────────────────────────

class TwinSyncRequest(BaseModel):
    project_path: str
    project_id: str = ""

class TwinFileRequest(BaseModel):
    abs_path: str
    project_path: str
    project_id: str = ""

class TwinQueryRequest(BaseModel):
    cypher: str
    params: Dict[str, Any] = {}

# ── Lazy-load helper ──────────────────────────────────────────────────────────

# Mutable container avoids `global` statements in nested functions.
_twin_state: dict = {"instance": None}

# Exception class names that indicate a stale/expired Neo4j driver.
# Checked by name to avoid importing neo4j at module load time.
_NEO4J_DRIVER_EXPIRY_EXCEPTIONS = frozenset({
    "ServiceUnavailable",
    "SessionExpired",
    "AuthError",
    "Neo4jError",
    "DriverError",
})

def _get_twin():
    """Lazy-load digital_twin module."""
    if _twin_state["instance"] is not None:
        return _twin_state["instance"]
    try:
        from ...digital_twin import DigitalTwin
        _twin_state["instance"] = DigitalTwin()
    except Exception as exc:
        log.debug("digital_twin_unavailable error=%s", exc)
        _twin_state["instance"] = None
    return _twin_state["instance"]

async def _twin_call(fn, *args, **kwargs):
    """
    Run a sync twin method in a thread.
    If Neo4j raises a driver-expiry exception, reset the singleton so the
    next request gets a fresh connection, then re-raise as 503.
    """
    try:
        return await asyncio.to_thread(fn, *args, **kwargs)
    except Exception as exc:
        if type(exc).__name__ in _NEO4J_DRIVER_EXPIRY_EXCEPTIONS:
            log.warning("twin_driver_reset reason=%s", type(exc).__name__)
            _twin_state["instance"] = None
            raise HTTPException(
                status_code=503,
                detail=f"Neo4j connection lost ({type(exc).__name__}); retry in a moment",
            )
        raise

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/twin/sync")
@circuit_breaker(name="neo4j")
async def twin_sync(
    body: TwinSyncRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Full project sync to digital twin."""
    twin = _get_twin()
    if not twin:
        raise HTTPException(status_code=503, detail="digital_twin module unavailable")

    sync_id = None
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO twin_sync_log
                        (project_id, tenant_id, sync_type, status)
                    VALUES (%s, %s, 'full', 'running') RETURNING id
                    """,
                    (body.project_id, tenant_id),
                )
                row = await cur.fetchone()
                sync_id = row["id"] if row else None
                await conn.commit()
    except Exception:
        log.warning("twin_sync_log_start_error", exc_info=True)

    stats = await _twin_call(twin.sync_project, body.project_path, body.project_id, tenant_id)

    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if sync_id:
                    await cur.execute(
                        """
                        UPDATE twin_sync_log
                        SET status = %s, files_scanned = %s, test_files = %s,
                            nodes_created = %s, edges_created = %s,
                            infra_services = %s, errors = %s,
                            finished_at = NOW()
                        WHERE id = %s
                        """,
                        (
                            "error" if stats.get("errors", 0) > 0 else "done",
                            stats.get("files", 0), stats.get("test_files", 0),
                            stats.get("nodes_created", 0), stats.get("edges_created", 0),
                            stats.get("infra_services", 0), stats.get("errors", 0),
                            sync_id,
                        ),
                    )
                    await conn.commit()
    except Exception:
        log.warning("twin_sync_log_update_error", exc_info=True)

    return {"sync_id": sync_id, **stats}

@router.post("/twin/sync/file")
@circuit_breaker(name="neo4j")
async def twin_sync_file(
    body: TwinFileRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Incremental single-file sync."""
    twin = _get_twin()
    if not twin:
        raise HTTPException(status_code=503, detail="digital_twin module unavailable")

    result = await _twin_call(twin.sync_file, body.abs_path, body.project_path, body.project_id, tenant_id)
    return result

@router.get("/twin/gaps")
@circuit_breaker(name="neo4j")
async def twin_gaps(
    project_id: str = "",
    tenant_id: str = Depends(get_tenant_id)
):
    """Gap analysis (missing tests, docs, owners)."""
    twin = _get_twin()
    if not twin:
        raise HTTPException(status_code=503, detail="digital_twin module unavailable")
    return await _twin_call(twin.gap_analysis, project_id, tenant_id)

@router.get("/twin/coupling")
@circuit_breaker(name="neo4j")
async def twin_coupling(
    project_id: str = "",
    min_dependents: int = Query(3, alias="min_dependents"),
    tenant_id: str = Depends(get_tenant_id)
):
    """Structural coupling analysis."""
    twin = _get_twin()
    if not twin:
        raise HTTPException(status_code=503, detail="digital_twin module unavailable")
    return await _twin_call(twin.coupling_analysis, project_id, tenant_id, min_dependents)

@router.get("/twin/status")
@circuit_breaker(name="neo4j")
async def twin_status(
    project_id: str = "",
    tenant_id: str = Depends(get_tenant_id)
):
    """Twin sync status for a project."""
    twin = _get_twin()
    if not twin:
        raise HTTPException(status_code=503, detail="digital_twin module unavailable")
    return await _twin_call(twin.status, project_id, tenant_id)

@router.post("/twin/query")
@circuit_breaker(name="neo4j")
async def twin_query(
    body: TwinQueryRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Execute a raw Cypher query against the twin graph."""
    twin = _get_twin()
    if not twin:
        raise HTTPException(status_code=503, detail="digital_twin module unavailable")

    try:
        rows = await _twin_call(twin.query, body.cypher, body.params)
        return {"rows": rows, "count": len(rows)}
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

@router.get("/twin/impact/{file_path:path}")
@circuit_breaker(name="neo4j")
async def twin_file_impact(
    file_path: str,
    project_id: str = "",
    tenant_id: str = Depends(get_tenant_id)
):
    """Transitive impact radius for a given file."""
    twin = _get_twin()
    if not twin:
        raise HTTPException(status_code=503, detail="digital_twin module unavailable")

    tasks = []
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                await cur.execute(
                    """
                    SELECT tfl.task_id, t.title, t.status, t.assigned_agent
                    FROM task_file_links tfl
                    LEFT JOIN tasks t ON t.{task_pk} = tfl.task_id
                    WHERE tfl.file_path = %s AND tfl.tenant_id = %s
                    ORDER BY tfl.linked_at DESC
                    LIMIT 20
                    """.format(task_pk=task_pk),
                    (file_path, tenant_id),
                )
                rows = await cur.fetchall()
                for row in rows:
                    tasks.append({
                        "task_id": row["task_id"],
                        "title":   row.get("title"),
                        "status":  row.get("status"),
                        "agent":   row.get("assigned_agent"),
                    })
    except Exception:
        log.warning("twin_file_tasks_lookup_error", exc_info=True)

    dependents = []
    try:
        dependents = await _twin_call(
            twin.query,
            """
            MATCH (src:File {path: $path})<-[:IMPORTS|CALLS|DEPENDS_ON*1..3]-(dep)
            WHERE src.project_id = $pid
            RETURN labels(dep)[0] AS type, dep.name AS name,
                   COALESCE(dep.path, dep.file) AS location
            LIMIT 50
            """,
            {"path": file_path, "pid": project_id},
        )
    except Exception:
        log.warning("twin_file_dependents_query_error", exc_info=True)

    return {
        "file":       file_path,
        "tasks":      tasks,
        "dependents": dependents,
    }
