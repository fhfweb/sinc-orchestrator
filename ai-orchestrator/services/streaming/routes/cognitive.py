from services.streaming.core.config import env_get
"""
streaming/routes/cognitive.py
==============================
FastAPI Router for Cognitive and Swarm operations.
"""
import logging
import asyncio
import os
import json
from uuid import uuid4
from typing import List, Optional, Dict, Any
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db
from services.streaming.core.schema_compat import get_task_pk_column
from services.memory_compaction import ensure_memory_compaction_schema, fetch_reactivation_hints
from services.otel_setup import current_trace_id, span

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["cognitive"])

# ── Models ───────────────────────────────────────────────────────────────────

class CognitiveProcessTask(BaseModel):
    id: Optional[str] = None
    task_type: Optional[str] = None
    title: str = ""
    description: str = ""
    project_id: Optional[str] = ""

class CognitiveBatchRequest(BaseModel):
    tasks: List[CognitiveProcessTask]

class AgentDescriptor(BaseModel):
    name: str
    active_tasks: int = 0
    queued_tasks: int = 0
    total_done: int = 0
    error_rate: float = 0.0
    last_seen_s: Optional[float] = None

class SwarmAssignRequest(BaseModel):
    task: Dict[str, Any]
    agents: Optional[List[AgentDescriptor]] = None

class SwarmRebalanceRequest(BaseModel):
    apply: bool = False

# ── Lazy singletons ───────────────────────────────────────────────────────────

_orchestrator = None
_scheduler    = None
_cognitive_batch_running: set[asyncio.Task] = set()

COGNITIVE_BATCH_MAX_TASKS_PER_JOB = int(env_get("ORCHESTRATOR_COGNITIVE_BATCH_MAX_TASKS_PER_JOB", default="16"))
COGNITIVE_BATCH_MAX_PENDING_PER_TENANT = int(env_get("ORCHESTRATOR_COGNITIVE_BATCH_MAX_PENDING_PER_TENANT", default="10"))
COGNITIVE_BATCH_MAX_RUNNING_PER_TENANT = int(env_get("ORCHESTRATOR_COGNITIVE_BATCH_MAX_RUNNING_PER_TENANT", default="2"))
COGNITIVE_BATCH_CONCURRENCY = int(env_get("ORCHESTRATOR_COGNITIVE_BATCH_CONCURRENCY", default="2"))
COGNITIVE_BATCH_POLL_INTERVAL_S = float(env_get("ORCHESTRATOR_COGNITIVE_BATCH_POLL_INTERVAL_SECONDS", default="3"))
COGNITIVE_BATCH_MAX_ESTIMATED_TOKENS_PER_JOB = int(
    env_get("ORCHESTRATOR_COGNITIVE_BATCH_MAX_ESTIMATED_TOKENS_PER_JOB", default="6000")
)
COGNITIVE_BATCH_MAX_PENDING_ESTIMATED_TOKENS_PER_TENANT = int(
    env_get("ORCHESTRATOR_COGNITIVE_BATCH_MAX_PENDING_ESTIMATED_TOKENS_PER_TENANT", default="24000")
)


def _estimate_cognitive_task_tokens(task: dict[str, Any]) -> int:
    title = str(task.get("title") or "")
    description = str(task.get("description") or "")
    task_type = str(task.get("task_type") or "generic")
    project_id = str(task.get("project_id") or "")
    text = " ".join(part for part in [title, description, task_type, project_id] if part).strip()
    base = max(32, int(len(text) / 4) + 24)
    return min(base, 2048)


def _estimate_cognitive_batch_tokens(tasks: list[dict[str, Any]]) -> int:
    return sum(_estimate_cognitive_task_tokens(task) for task in tasks)


async def ensure_cognitive_batch_schema() -> None:
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cognitive_batch_jobs (
                    batch_job_id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    task_count INTEGER NOT NULL DEFAULT 0,
                    llm_used_count INTEGER NOT NULL DEFAULT 0,
                    cache_hit_count INTEGER NOT NULL DEFAULT 0,
                    total_latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                    estimated_tokens INTEGER NOT NULL DEFAULT 0,
                    queue_position INTEGER NOT NULL DEFAULT 0,
                    claimed_by TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    state_message TEXT NOT NULL DEFAULT '',
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    started_at TIMESTAMPTZ,
                    completed_at TIMESTAMPTZ
                )
                """
            )
            await cur.execute(
                """
                CREATE TABLE IF NOT EXISTS cognitive_batch_job_items (
                    batch_job_item_id BIGSERIAL PRIMARY KEY,
                    batch_job_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    ordinal INTEGER NOT NULL DEFAULT 0,
                    task_id TEXT,
                    task_type TEXT NOT NULL DEFAULT 'generic',
                    title TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'unknown',
                    planner TEXT NOT NULL DEFAULT 'unknown',
                    cache_level TEXT NOT NULL DEFAULT 'none',
                    llm_used BOOLEAN NOT NULL DEFAULT FALSE,
                    tokens_used INTEGER NOT NULL DEFAULT 0,
                    estimated_tokens INTEGER NOT NULL DEFAULT 0,
                    latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                    error TEXT,
                    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            await cur.execute(
                "ALTER TABLE cognitive_batch_jobs ADD COLUMN IF NOT EXISTS estimated_tokens INTEGER NOT NULL DEFAULT 0"
            )
            await cur.execute(
                "ALTER TABLE cognitive_batch_jobs ADD COLUMN IF NOT EXISTS queue_position INTEGER NOT NULL DEFAULT 0"
            )
            await cur.execute(
                "ALTER TABLE cognitive_batch_jobs ADD COLUMN IF NOT EXISTS claimed_by TEXT NOT NULL DEFAULT ''"
            )
            await cur.execute(
                "ALTER TABLE cognitive_batch_jobs ADD COLUMN IF NOT EXISTS attempt_count INTEGER NOT NULL DEFAULT 0"
            )
            await cur.execute(
                "ALTER TABLE cognitive_batch_jobs ADD COLUMN IF NOT EXISTS state_message TEXT NOT NULL DEFAULT ''"
            )
            await cur.execute(
                "ALTER TABLE cognitive_batch_job_items ADD COLUMN IF NOT EXISTS estimated_tokens INTEGER NOT NULL DEFAULT 0"
            )
            await cur.execute(
                "ALTER TABLE cognitive_batch_job_items ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()"
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cognitive_batch_jobs_tenant_created
                    ON cognitive_batch_jobs (tenant_id, created_at DESC)
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cognitive_batch_jobs_status_created
                    ON cognitive_batch_jobs (status, created_at ASC)
                """
            )
            await cur.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_cognitive_batch_job_items_job
                    ON cognitive_batch_job_items (batch_job_id, ordinal)
                """
            )
        await conn.commit()


async def _count_cognitive_batch_jobs_for_tenant(
    tenant_id: str,
    *,
    statuses: tuple[str, ...] = ("pending", "running"),
) -> int:
    await ensure_cognitive_batch_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COUNT(*) AS total
                  FROM cognitive_batch_jobs
                 WHERE tenant_id = %s
                   AND status = ANY(%s)
                """,
                (tenant_id, list(statuses)),
            )
            row = await cur.fetchone()
    return int((row or {}).get("total") or 0)


async def _sum_cognitive_batch_estimated_tokens_for_tenant(
    tenant_id: str,
    *,
    statuses: tuple[str, ...] = ("pending", "running"),
) -> int:
    await ensure_cognitive_batch_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT COALESCE(SUM(estimated_tokens), 0) AS total
                  FROM cognitive_batch_jobs
                 WHERE tenant_id = %s
                   AND status = ANY(%s)
                """,
                (tenant_id, list(statuses)),
            )
            row = await cur.fetchone()
    return int((row or {}).get("total") or 0)


async def _create_cognitive_batch_job(
    batch_job_id: str,
    tenant_id: str,
    tasks: list[dict[str, Any]],
    *,
    estimated_tokens: int,
) -> int:
    await ensure_cognitive_batch_schema()
    queue_position = await _count_cognitive_batch_jobs_for_tenant(tenant_id, statuses=("pending",)) + 1
    trace_id = current_trace_id()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            with span(
                "cognitive.batch.enqueue",
                batch_job_id=batch_job_id,
                tenant_id=tenant_id,
                task_count=len(tasks),
                estimated_tokens=estimated_tokens,
                queue_position=queue_position,
                trace_id=trace_id,
            ):
                await cur.execute(
                    """
                    INSERT INTO cognitive_batch_jobs
                        (batch_job_id, tenant_id, status, task_count, estimated_tokens, queue_position, state_message, metadata)
                    VALUES (%s, %s, 'pending', %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        batch_job_id,
                        tenant_id,
                        len(tasks),
                        estimated_tokens,
                        queue_position,
                        "queued",
                        json.dumps(
                            {
                                "task_ids": [t.get("id") for t in tasks],
                                "task_types": [t.get("task_type") or "generic" for t in tasks],
                                "trace_id": trace_id,
                            }
                        ),
                    ),
                )
            for ordinal, task in enumerate(tasks, start=1):
                await cur.execute(
                    """
                    INSERT INTO cognitive_batch_job_items
                        (batch_job_id, tenant_id, ordinal, task_id, task_type, title, status, estimated_tokens, metadata)
                    VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s::jsonb)
                    """,
                    (
                        batch_job_id,
                        tenant_id,
                        ordinal,
                        task.get("id"),
                        task.get("task_type") or "generic",
                        task.get("title") or task.get("description") or "",
                        _estimate_cognitive_task_tokens(task),
                        json.dumps({"input": task}),
                    ),
                )
        await conn.commit()
    return queue_position


async def _complete_cognitive_batch_job(
    batch_job_id: str,
    tenant_id: str,
    *,
    status: str,
    results: list[dict[str, Any]] | None = None,
    error: str = "",
    state_message: str = "",
) -> None:
    await ensure_cognitive_batch_schema()
    results = results or []
    metadata = {"error": error} if error else {}
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                UPDATE cognitive_batch_jobs
                   SET status = %s,
                       llm_used_count = %s,
                       cache_hit_count = %s,
                       total_latency_ms = %s,
                       queue_position = 0,
                       state_message = %s,
                       metadata = metadata || %s::jsonb,
                       completed_at = NOW()
                 WHERE batch_job_id = %s
                   AND tenant_id = %s
                """,
                (
                    status,
                    sum(1 for row in results if row.get("llm_used")),
                    sum(1 for row in results if not row.get("llm_used")),
                    sum(float(row.get("latency_ms") or 0.0) for row in results),
                    state_message or status,
                    json.dumps(metadata),
                    batch_job_id,
                    tenant_id,
                ),
            )
        await conn.commit()


async def _persist_cognitive_batch_items(batch_job_id: str, tenant_id: str, tasks: list[dict[str, Any]], results: list[dict[str, Any]]) -> None:
    await ensure_cognitive_batch_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            for ordinal, (task, row) in enumerate(zip(tasks, results), start=1):
                await cur.execute(
                    """
                    UPDATE cognitive_batch_job_items
                       SET status = %s,
                           planner = %s,
                           cache_level = %s,
                           llm_used = %s,
                           tokens_used = %s,
                           latency_ms = %s,
                           error = %s,
                           metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb,
                           updated_at = NOW()
                     WHERE batch_job_id = %s
                       AND tenant_id = %s
                       AND ordinal = %s
                    """,
                    (
                        "completed" if not row.get("error") else "failed",
                        row.get("planner") or "unknown",
                        row.get("cache_level") or "none",
                        bool(row.get("llm_used", False)),
                        int(row.get("tokens_used") or 0),
                        float(row.get("latency_ms") or 0.0),
                        row.get("error"),
                        json.dumps({"input": task, "output": row}),
                        batch_job_id,
                        tenant_id,
                        ordinal,
                    ),
                )
        await conn.commit()


async def _fetch_cognitive_batch_tasks(batch_job_id: str, tenant_id: str) -> list[dict[str, Any]]:
    await ensure_cognitive_batch_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT ordinal, task_id, task_type, title, metadata
                  FROM cognitive_batch_job_items
                 WHERE batch_job_id = %s
                   AND tenant_id = %s
                 ORDER BY ordinal
                """,
                (batch_job_id, tenant_id),
            )
            rows = await cur.fetchall()
    tasks: list[dict[str, Any]] = []
    for row in rows:
        metadata = dict(row.get("metadata") or {})
        input_payload = dict(metadata.get("input") or {})
        input_payload.setdefault("id", row.get("task_id"))
        input_payload.setdefault("task_type", row.get("task_type"))
        input_payload.setdefault("title", row.get("title") or "")
        input_payload.setdefault("tenant_id", tenant_id)
        tasks.append(input_payload)
    return tasks


async def _fetch_cognitive_batch_job(batch_job_id: str, tenant_id: str) -> dict[str, Any] | None:
    await ensure_cognitive_batch_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT *
                  FROM cognitive_batch_jobs
                 WHERE batch_job_id = %s
                   AND tenant_id = %s
                """,
                (batch_job_id, tenant_id),
            )
            job = await cur.fetchone()
            if not job:
                return None
            await cur.execute(
                """
                SELECT ordinal, task_id, task_type, title, status, planner, cache_level,
                       llm_used, tokens_used, estimated_tokens, latency_ms, error, metadata, created_at
                  FROM cognitive_batch_job_items
                 WHERE batch_job_id = %s
                   AND tenant_id = %s
                 ORDER BY ordinal
                """,
                (batch_job_id, tenant_id),
            )
            items = [dict(row) for row in await cur.fetchall()]
    return {"job": dict(job), "items": items}


async def _claim_next_cognitive_batch_job(worker_id: str) -> dict[str, Any] | None:
    await ensure_cognitive_batch_schema()
    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            with span("cognitive.batch.claim", worker_id=worker_id, max_running_per_tenant=COGNITIVE_BATCH_MAX_RUNNING_PER_TENANT):
                await cur.execute(
                    """
                    WITH candidate AS (
                        SELECT batch_job_id, tenant_id
                          FROM cognitive_batch_jobs j
                         WHERE j.status = 'pending'
                           AND (
                                SELECT COUNT(*)
                                  FROM cognitive_batch_jobs r
                                 WHERE r.tenant_id = j.tenant_id
                                   AND r.status = 'running'
                           ) < %s
                         ORDER BY j.created_at ASC
                         FOR UPDATE SKIP LOCKED
                         LIMIT 1
                    )
                    UPDATE cognitive_batch_jobs j
                       SET status = 'running',
                           started_at = NOW(),
                           claimed_by = %s,
                           attempt_count = attempt_count + 1,
                           state_message = 'processing',
                           queue_position = 0
                      FROM candidate
                     WHERE j.batch_job_id = candidate.batch_job_id
                     RETURNING j.batch_job_id, j.tenant_id
                    """,
                    (COGNITIVE_BATCH_MAX_RUNNING_PER_TENANT, worker_id),
                )
            row = await cur.fetchone()
        await conn.commit()
    return dict(row) if row else None


async def _execute_cognitive_batch_job(batch_job_id: str, tenant_id: str) -> dict[str, Any]:
    orch = _get_orchestrator()
    if not orch:
        await _complete_cognitive_batch_job(
            batch_job_id,
            tenant_id,
            status="failed",
            error="cognitive_orchestrator unavailable",
            state_message="failed: orchestrator unavailable",
        )
        return {"batch_job_id": batch_job_id, "status": "failed"}

    tasks = await _fetch_cognitive_batch_tasks(batch_job_id, tenant_id)
    if not tasks:
        await _complete_cognitive_batch_job(
            batch_job_id,
            tenant_id,
            status="failed",
            error="queued batch has no tasks",
            state_message="failed: empty batch",
        )
        return {"batch_job_id": batch_job_id, "status": "failed"}

    try:
        with span("cognitive.batch.execute", batch_job_id=batch_job_id, tenant_id=tenant_id, task_count=len(tasks)):
            results = await orch.process_batch(tasks)
        serialized_results = [
            {
                "task_id": getattr(r, "task_id", None),
                "solution": getattr(r, "solution", None),
                "planner": getattr(r, "planner", "unknown"),
                "cache_level": getattr(r, "cache_level", "none"),
                "llm_used": getattr(r, "llm_used", False),
                "tokens_used": getattr(r, "tokens_used", 0),
                "latency_ms": getattr(r, "latency_ms", 0),
                "error": getattr(r, "error", None),
            }
            for r in results
        ]
        await _persist_cognitive_batch_items(batch_job_id, tenant_id, tasks, serialized_results)
        await _complete_cognitive_batch_job(
            batch_job_id,
            tenant_id,
            status="completed",
            results=serialized_results,
            state_message="completed",
        )
        return {
            "batch_job_id": batch_job_id,
            "status": "completed",
            "results": serialized_results,
        }
    except Exception as exc:
        await _complete_cognitive_batch_job(
            batch_job_id,
            tenant_id,
            status="failed",
            error=str(exc),
            state_message=f"failed: {exc}",
        )
        return {"batch_job_id": batch_job_id, "status": "failed", "error": str(exc)}


async def run_cognitive_batch_queue_once() -> int:
    worker_id = f"embedded-{os.getpid()}"
    finished = {task for task in _cognitive_batch_running if task.done()}
    for task in finished:
        _cognitive_batch_running.discard(task)
    started = 0
    while len(_cognitive_batch_running) < COGNITIVE_BATCH_CONCURRENCY:
        claimed = await _claim_next_cognitive_batch_job(worker_id)
        if not claimed:
            break
        started += 1

        task_ref = asyncio.create_task(
            _execute_cognitive_batch_job(claimed["batch_job_id"], claimed["tenant_id"]),
            name=f"cognitive-batch:{claimed['batch_job_id']}",
        )
        _cognitive_batch_running.add(task_ref)
        task_ref.add_done_callback(lambda done: _cognitive_batch_running.discard(done))
    return started


async def run_cognitive_batch_queue_loop() -> None:
    while True:
        try:
            await run_cognitive_batch_queue_once()
        except Exception as exc:
            log.warning("cognitive_batch_queue_tick_failed error=%s", exc)
        await asyncio.sleep(COGNITIVE_BATCH_POLL_INTERVAL_S)

def _get_orchestrator():
    global _orchestrator
    if _orchestrator is None:
        try:
            from ...cognitive_orchestrator import get_orchestrator
            _orchestrator = get_orchestrator()
        except Exception as exc:
            log.warning("cognitive_orchestrator_unavailable error=%s", exc)
    return _orchestrator

def _get_scheduler():
    global _scheduler
    if _scheduler is None:
        try:
            from ...agent_swarm import get_scheduler
            _scheduler = get_scheduler()
        except Exception as exc:
            log.warning("swarm_scheduler_unavailable error=%s", exc)
    return _scheduler

# ── POST /cognitive/process ───────────────────────────────────────────────────

@router.post("/cognitive/process")
async def cognitive_process(
    body: CognitiveProcessTask,
    tenant_id: str = Depends(get_tenant_id)
):
    """Run a single task through the full cognitive pipeline."""
    orch = _get_orchestrator()
    if not orch:
        raise HTTPException(status_code=503, detail="cognitive_orchestrator unavailable")

    if not body.description and not body.title:
        raise HTTPException(status_code=400, detail="description or title required")

    data = body.model_dump()
    data["id"] = data.get("id") or f"cog-{uuid4().hex[:12]}"
    data["tenant_id"] = tenant_id

    # Native async call
    result = await orch.process(data)
    
    return {
        "task_id":      getattr(result, "task_id", None),
        "solution":     getattr(result, "solution", None),
        "steps":        getattr(result, "steps", []),
        "planner":      getattr(result, "planner", "unknown"),
        "cache_level":  getattr(result, "cache_level", 0),
        "llm_used":     getattr(result, "llm_used", False),
        "tokens_saved": getattr(result, "tokens_saved", 0),
        "tokens_used":  getattr(result, "tokens_used", 0),
        "latency_ms":   getattr(result, "latency_ms", 0),
        "error":        getattr(result, "error", None),
    }

# ── POST /cognitive/batch ─────────────────────────────────────────────────────

@router.post("/cognitive/batch", status_code=status.HTTP_202_ACCEPTED)
async def cognitive_batch(
    body: CognitiveBatchRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Queue a batch of tasks for async semantic grouping and processing."""
    if len(body.tasks) > COGNITIVE_BATCH_MAX_TASKS_PER_JOB:
        raise HTTPException(
            status_code=429,
            detail=(
                f"cognitive batch quota exceeded: max_tasks_per_job="
                f"{COGNITIVE_BATCH_MAX_TASKS_PER_JOB}"
            ),
        )

    pending_for_tenant = await _count_cognitive_batch_jobs_for_tenant(tenant_id, statuses=("pending",))
    if pending_for_tenant >= COGNITIVE_BATCH_MAX_PENDING_PER_TENANT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"cognitive batch queue is full for tenant={tenant_id}: "
                f"pending={pending_for_tenant}"
            ),
        )

    tasks = [t.model_dump() for t in body.tasks]
    for t in tasks:
        t["id"] = t.get("id") or f"cog-{uuid4().hex[:12]}"
        t["tenant_id"] = tenant_id
    estimated_tokens = _estimate_cognitive_batch_tokens(tasks)

    if estimated_tokens > COGNITIVE_BATCH_MAX_ESTIMATED_TOKENS_PER_JOB:
        raise HTTPException(
            status_code=429,
            detail=(
                f"cognitive batch token quota exceeded: estimated_tokens={estimated_tokens} "
                f"max_estimated_tokens_per_job={COGNITIVE_BATCH_MAX_ESTIMATED_TOKENS_PER_JOB}"
            ),
        )

    batch_job_id = f"cbatch-{uuid4().hex[:12]}"
    pending_estimated_tokens = await _sum_cognitive_batch_estimated_tokens_for_tenant(
        tenant_id,
        statuses=("pending",),
    )
    if pending_estimated_tokens + estimated_tokens > COGNITIVE_BATCH_MAX_PENDING_ESTIMATED_TOKENS_PER_TENANT:
        raise HTTPException(
            status_code=429,
            detail=(
                f"cognitive batch pending token budget exceeded for tenant={tenant_id}: "
                f"pending_tokens={pending_estimated_tokens} requested_tokens={estimated_tokens}"
            ),
        )

    queue_position = await _create_cognitive_batch_job(
        batch_job_id,
        tenant_id,
        tasks,
        estimated_tokens=estimated_tokens,
    )

    return {
        "batch_job_id": batch_job_id,
        "status": "pending",
        "total": len(tasks),
        "estimated_tokens": estimated_tokens,
        "queue_position": queue_position,
        "quota": {
            "max_tasks_per_job": COGNITIVE_BATCH_MAX_TASKS_PER_JOB,
            "max_pending_per_tenant": COGNITIVE_BATCH_MAX_PENDING_PER_TENANT,
            "max_running_per_tenant": COGNITIVE_BATCH_MAX_RUNNING_PER_TENANT,
            "concurrency": COGNITIVE_BATCH_CONCURRENCY,
            "max_estimated_tokens_per_job": COGNITIVE_BATCH_MAX_ESTIMATED_TOKENS_PER_JOB,
            "max_pending_estimated_tokens_per_tenant": COGNITIVE_BATCH_MAX_PENDING_ESTIMATED_TOKENS_PER_TENANT,
        },
        "observability": {
            "status": "pending",
            "total_latency_ms": 0,
            "created_at": datetime.now(timezone.utc).isoformat(),
        },
    }


@router.get("/cognitive/batch/{batch_job_id}")
async def cognitive_batch_status(
    batch_job_id: str,
    tenant_id: str = Depends(get_tenant_id),
):
    payload = await _fetch_cognitive_batch_job(batch_job_id, tenant_id)
    if not payload:
        raise HTTPException(status_code=404, detail="cognitive batch job not found")
    return payload


@router.get("/cognitive/batches")
async def list_cognitive_batches(
    limit: int = Query(20, ge=1, le=100),
    tenant_id: str = Depends(get_tenant_id),
):
    await ensure_cognitive_batch_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT batch_job_id, status, task_count, llm_used_count, cache_hit_count,
                       total_latency_ms, estimated_tokens, queue_position, attempt_count, state_message,
                       created_at, started_at, completed_at
                  FROM cognitive_batch_jobs
                 WHERE tenant_id = %s
                 ORDER BY created_at DESC
                LIMIT %s
                """,
                (tenant_id, limit),
            )
            rows = [dict(row) for row in await cur.fetchall()]
    return {"items": rows, "count": len(rows)}


@router.get("/cognitive/memory/reactivation")
async def cognitive_memory_reactivation(
    project_id: str = Query("", max_length=120),
    task_type: str = Query("", max_length=120),
    file_path: str = Query("", max_length=400),
    incident_family: str = Query("", max_length=120),
    limit: int = Query(5, ge=1, le=20),
    tenant_id: str = Depends(get_tenant_id),
):
    await ensure_memory_compaction_schema()
    hints = await fetch_reactivation_hints(
        tenant_id=tenant_id,
        project_id=project_id,
        task_type=task_type,
        file_path=file_path,
        incident_family=incident_family,
        limit=limit,
    )
    return {"items": hints, "count": len(hints)}

# ── GET /cognitive/stats ──────────────────────────────────────────────────────

@router.get("/cognitive/stats")
async def cognitive_stats(tenant_id: str = Depends(get_tenant_id)):
    orch = _get_orchestrator()
    if not orch:
        raise HTTPException(status_code=503, detail="cognitive_orchestrator unavailable")
    return orch.get_stats()

# ── POST /swarm/assign ────────────────────────────────────────────────────────

@router.post("/swarm/assign")
async def swarm_assign(
    body: SwarmAssignRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    sched = _get_scheduler()
    if not sched:
        raise HTTPException(status_code=503, detail="swarm_scheduler unavailable")

    if body.agents:
        from ...agent_swarm import AgentState
        import time as _time
        agents = [
            AgentState(
                name         = a.name,
                active_tasks = a.active_tasks,
                queued_tasks = a.queued_tasks,
                total_done   = a.total_done,
                error_rate   = a.error_rate,
                last_seen_s  = a.last_seen_s or _time.time(),
            )
            for a in body.agents
        ]
    else:
        agents = await asyncio.to_thread(sched.load_agents_from_db, tenant_id)

    assignment = await asyncio.to_thread(sched.assign, body.task, agents)
    if not assignment:
        raise HTTPException(status_code=409, detail="no suitable agent available")

    return {
        "task_id":    assignment.task_id,
        "agent_name": assignment.agent_name,
        "score":      assignment.score,
        "reason":     assignment.reason,
    }

# ── POST /swarm/rebalance ─────────────────────────────────────────────────────

@router.post("/swarm/rebalance")
async def swarm_rebalance(
    body: SwarmRebalanceRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    sched = _get_scheduler()
    if not sched:
        raise HTTPException(status_code=503, detail="swarm_scheduler unavailable")

    agents = await asyncio.to_thread(sched.load_agents_from_db, tenant_id)
    tasks  = await asyncio.to_thread(sched.load_pending_tasks_from_db, tenant_id)

    assignments = await asyncio.to_thread(sched.rebalance, agents, tasks)

    if body.apply and assignments:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                for a in assignments:
                    task_pk = await get_task_pk_column(cur)
                    await cur.execute(
                        "UPDATE tasks SET assigned_agent = %s, updated_at = NOW() "
                        f"WHERE {task_pk} = %s AND tenant_id = %s AND assigned_agent IS NULL",
                        (a.agent_name, a.task_id, tenant_id),
                    )
            await conn.commit()

    return {
        "assignments": [
            {"task_id": a.task_id, "agent_name": a.agent_name,
             "score": a.score, "reason": a.reason}
            for a in assignments
        ],
        "total":   len(assignments),
        "applied": body.apply,
    }

# ── GET /swarm/workload ───────────────────────────────────────────────────────

@router.get("/swarm/workload")
async def swarm_workload(tenant_id: str = Depends(get_tenant_id)):
    sched = _get_scheduler()
    if not sched:
        raise HTTPException(status_code=503, detail="swarm_scheduler unavailable")
    agents = await asyncio.to_thread(sched.load_agents_from_db, tenant_id)
    return await asyncio.to_thread(sched.workload_report, agents)

# ── GET /cognitive/obs ────────────────────────────────────────────────────────

@router.get("/cognitive/obs")
async def cognitive_obs(tenant_id: str = Depends(get_tenant_id)):
    orch = _get_orchestrator()
    if not orch:
        raise HTTPException(status_code=503, detail="cognitive_orchestrator unavailable")
    obs = getattr(orch, "_obs", None)
    if not obs:
        return orch.get_stats()
    return await asyncio.to_thread(obs.report_savings)

# ── GET /cognitive/rules ──────────────────────────────────────────────────────

@router.get("/cognitive/rules")
async def cognitive_rules(tenant_id: str = Depends(get_tenant_id)):
    orch = _get_orchestrator()
    if not orch:
        raise HTTPException(status_code=503, detail="cognitive_orchestrator unavailable")
    rules_engine = getattr(orch, "_rules", None)
    if not rules_engine:
        return {"rules": [], "count": 0, "note": "DynamicRuleEngine unavailable"}
    rules = await asyncio.to_thread(rules_engine.get_all_rules)
    return {"rules": rules, "count": len(rules)}

# ── POST /cognitive/rules/learn ───────────────────────────────────────────────

@router.post("/cognitive/rules/learn")
async def cognitive_rules_learn(
    tenant_id: Optional[str] = None,
    current_tenant: str = Depends(get_tenant_id)
):
    target_tenant = tenant_id or current_tenant
    orch = _get_orchestrator()
    if not orch:
        raise HTTPException(status_code=503, detail="cognitive_orchestrator unavailable")
    rules_engine = getattr(orch, "_rules", None)
    if not rules_engine:
        raise HTTPException(status_code=503, detail="DynamicRuleEngine unavailable")

    before = len(rules_engine._rules)
    await asyncio.to_thread(rules_engine.learn_rules_from_history, target_tenant)
    after = len(rules_engine._rules)
    
    return {
        "ok": True,
        "rules_before": before,
        "rules_after": after,
        "new_rules": after - before,
    }

# ── GET /cognitive/got/stats ──────────────────────────────────────────────────

@router.get("/cognitive/got/stats")
async def cognitive_got_stats(tenant_id: str = Depends(get_tenant_id)):
    orch = _get_orchestrator()
    if not orch:
        raise HTTPException(status_code=503, detail="cognitive_orchestrator unavailable")
    got = getattr(orch, "_got", None)
    if not got:
        raise HTTPException(status_code=503, detail="GraphOfThought unavailable")

    try:
        def _sync_neo4j():
            driver = got._get_driver()
            with driver.session() as session:
                result = session.run("""
                    MATCH (n) RETURN labels(n)[0] AS label, count(n) AS cnt
                    UNION ALL
                    MATCH ()-[r]->() RETURN type(r) AS label, count(r) AS cnt
                """)
                return {r["label"]: r["cnt"] for r in result}
        
        counts = await asyncio.to_thread(_sync_neo4j)
        return {"nodes_edges": counts, "status": "connected"}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=str(exc))
