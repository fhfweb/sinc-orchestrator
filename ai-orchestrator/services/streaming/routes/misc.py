from services.streaming.core.config import env_get
"""
streaming/routes/misc.py
========================
FastAPI Router for Utilities (Whiteboard, Loop, Lessons, Signup, GitWebhooks).
"""
import hashlib
import hmac
import json
import logging
import os
import secrets
import asyncio
from datetime import datetime
from typing import List, Optional, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, Response, BackgroundTasks
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant, now_iso
from services.streaming.core.db import async_db
from services.streaming.core.sse import broadcast
from services.streaming.core.config import DISPATCHES
from services.streaming.core.runtime_plane import ensure_runtime_plane_schema
from services.streaming.core.state_plane import (
    announce_whiteboard_entry,
    get_whiteboard_snapshot,
)
from .ingest import _run_ingest_pipeline, IngestRequest

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["misc"])

_SIGNUP_ENABLED = env_get("SIGNUP_ENABLED", default="true").lower() in ("1", "true", "yes")

# ── Models ───────────────────────────────────────────────────────────────────

class WhiteboardAnnounceRequest(BaseModel):
    task_id: str
    agent: str
    intention: str
    files_intended: List[str] = []

class LoopStatePushRequest(BaseModel):
    project_id: str
    cycle: int = 0
    phase: str = ""
    status: str = "running"
    summary: str = ""
    metadata: Dict[str, Any] = {}

class PolicyReportPushRequest(BaseModel):
    project_id: str
    report: Dict[str, Any] = {}
    violations: int = 0
    status: str = "ok"

class LessonRecordRequest(BaseModel):
    error_signature: str
    attempted_fix: str
    result: str
    confidence: float = 1.0
    agent_name: str = ""
    task_id: str = ""
    project_id: str = ""
    context: str = ""

class SignupRequest(BaseModel):
    name: str
    plan: str = "free"
    email: str = ""

# ── Helpers ───────────────────────────────────────────────────────────────────

async def _check_rate_limit_async(key: str, rpm: int) -> bool:
    try:
        from services.streaming.core.redis_ import check_rate_limit_async as _rl
        return await _rl(key, rpm)
    except Exception:
        return True

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/whiteboard")
async def get_whiteboard(tenant_id: str = Depends(get_tenant_id)):
    return await get_whiteboard_snapshot(tenant_id)

@router.post("/whiteboard/announce")
async def whiteboard_announce(
    body: WhiteboardAnnounceRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Agents announce intent before acquiring a lock."""
    await announce_whiteboard_entry(
        tenant_id=tenant_id,
        task_id=body.task_id,
        agent_name=body.agent,
        intention=body.intention,
        files_intended=body.files_intended,
    )
    await broadcast("whiteboard_announced", {
        "task_id": body.task_id, "agent": body.agent,
    }, tenant_id=tenant_id)
    return {"ok": True}

@router.post("/loop/state")
async def push_loop_state(
    body: LoopStatePushRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Upsert current loop cycle state."""
    await ensure_runtime_plane_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO loop_states
                    (tenant_id, project_id, cycle, phase, status, summary, metadata, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())
                ON CONFLICT (tenant_id, project_id) DO UPDATE SET
                    cycle = EXCLUDED.cycle, phase = EXCLUDED.phase,
                    status = EXCLUDED.status, summary = EXCLUDED.summary,
                    metadata = EXCLUDED.metadata, updated_at = NOW()
                """,
                (tenant_id, body.project_id, body.cycle, body.phase, body.status,
                 body.summary, json.dumps(body.metadata)),
            )
            await conn.commit()
    
    await broadcast("loop_state_updated", {
        "tenant_id": tenant_id, "project_id": body.project_id,
        "cycle": body.cycle, "phase": body.phase, "status": body.status,
    }, tenant_id=tenant_id)
    return {"ok": True}

@router.get("/loop/state")
async def get_loop_state(
    project_id: Optional[str] = None,
    tenant_id: str = Depends(get_tenant_id)
):
    """Get current loop state."""
    await ensure_runtime_plane_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            if project_id:
                await cur.execute(
                    "SELECT * FROM loop_states WHERE tenant_id = %s AND project_id = %s",
                    (tenant_id, project_id),
                )
                row = await cur.fetchone()
                return row or {}
            else:
                await cur.execute(
                    "SELECT * FROM loop_states WHERE tenant_id = %s "
                    "ORDER BY updated_at DESC LIMIT 20",
                    (tenant_id,),
                )
                rows = await cur.fetchall()
                return {"loop_states": rows}

@router.post("/reports/policy/push")
async def push_policy_report(
    body: PolicyReportPushRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Push a governance report."""
    await ensure_runtime_plane_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO policy_reports (tenant_id, project_id, report, violations, status)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (tenant_id, body.project_id, json.dumps(body.report), body.violations, body.status),
            )
            await conn.commit()
    
    if body.violations > 0:
        await broadcast("policy_violation", {
            "tenant_id": tenant_id, "project_id": body.project_id,
            "violations": body.violations, "status": body.status,
        }, tenant_id=tenant_id)
    return {"ok": True}

@router.get("/reports/policy")
async def get_policy_report(
    project_id: Optional[str] = None,
    tenant_id: str = Depends(get_tenant_id)
):
    """Latest policy report for the tenant."""
    await ensure_runtime_plane_schema()
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            if project_id:
                await cur.execute(
                    "SELECT * FROM policy_reports WHERE tenant_id = %s AND project_id = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (tenant_id, project_id),
                )
            else:
                await cur.execute(
                    "SELECT * FROM policy_reports WHERE tenant_id = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (tenant_id,),
                )
            row = await cur.fetchone()
    return row or {}

@router.post("/lessons")
async def record_lesson(
    body: LessonRecordRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Record a lesson learned from a task execution."""
    if body.result not in ("success", "failure"):
        raise HTTPException(status_code=400, detail="result must be success or failure")
    
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO lessons_learned
                    (tenant_id, project_id, error_signature, context,
                     attempted_fix, result, confidence, agent_name, task_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (tenant_id, body.project_id, body.error_signature, body.context,
                 body.attempted_fix, body.result, body.confidence, 
                 body.agent_name, body.task_id),
            )
            row = await cur.fetchone()
            lesson_id = row["id"]
            await conn.commit()
    
    await broadcast("lesson_recorded", {
        "id": lesson_id, "signature": body.error_signature, "result": body.result,
    }, tenant_id=tenant_id)
    return {"ok": True, "id": lesson_id}

@router.get("/lessons")
async def query_lessons(
    signature: str = "",
    result: str = "",
    limit: int = Query(10, ge=1, le=50),
    tenant_id: str = Depends(get_tenant_id)
):
    """Query lessons learned."""
    conditions: List[str] = ["tenant_id = %s"]
    params: List = [tenant_id]
    if signature:
        conditions.append("error_signature ILIKE %s")
        params.append(f"%{signature}%")
    if result:
        conditions.append("result = %s")
        params.append(result)
    
    where = " AND ".join(conditions)
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT id, error_signature, context, attempted_fix,
                       result, confidence, agent_name, task_id, created_at
                FROM lessons_learned
                WHERE {where}
                ORDER BY confidence DESC, created_at DESC
                LIMIT %s
                """,
                params + [limit],
            )
            rows = await cur.fetchall()
    return {"lessons": rows, "count": len(rows)}

@router.post("/signup", status_code=status.HTTP_201_CREATED)
async def signup(
    body: SignupRequest,
    request: Request
):
    """Self-service tenant provisioning."""
    await ensure_runtime_plane_schema()
    if not _SIGNUP_ENABLED:
        raise HTTPException(status_code=403, detail="Self-service signup is disabled")

    if body.plan not in ("free", "pro", "enterprise"):
        raise HTTPException(status_code=400, detail="Invalid plan")

    remote_ip = request.client.host if request.client else "unknown"
    if not await _check_rate_limit_async(f"signup:{remote_ip}", 10):
        raise HTTPException(status_code=429, detail="Too many signup requests")

    tid = body.name.lower().replace(" ", "-")[:32] + "-" + os.urandom(3).hex()
    api_key = f"sk-{secrets.token_urlsafe(24)}"
    rpm = {"free": 10, "pro": 120, "enterprise": 600}.get(body.plan, 10)
    tpd = {"free": 50000, "pro": 1000000, "enterprise": 10000000}.get(body.plan, 50000)
    webhook_secret = secrets.token_hex(32)

    async with async_db(tenant_id="public") as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO tenants
                    (id, name, api_key, plan, requests_per_minute, tokens_per_day,
                     webhook_secret, metadata)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id, name, plan, api_key
                """,
                (tid, body.name, api_key, body.plan, rpm, tpd,
                 webhook_secret, json.dumps({"email": body.email, "signup_ip": remote_ip})),
            )
            await cur.execute(
                "INSERT INTO api_keys (id, tenant_id, key, name) VALUES (%s, %s, %s, 'primary') "
                "ON CONFLICT (key) DO NOTHING",
                (f"ak-{tid}", tid, api_key),
            )
            await conn.commit()
    
    return {"ok": True, "tenant_id": tid, "api_key": api_key, "plan": body.plan, "name": body.name}

@router.post("/webhooks/git", status_code=status.HTTP_202_ACCEPTED)
async def git_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    project_id: str = Query(...),
    tenant_id_override: Optional[str] = Query(None, alias="tenant_id"),
    tenant_id: str = Depends(get_tenant_id),
    tenant_obj: Dict[str, Any] = Depends(get_tenant)
):
    """Git push webhooks receiver."""
    target_tenant = tenant_id_override or tenant_id
    
    # Secret verification
    webhook_secret = tenant_obj.get("webhook_secret") or env_get("GIT_WEBHOOK_SECRET", default="")
    if webhook_secret:
        body_bytes = await request.body()
        sig_header = request.headers.get("X-Hub-Signature-256") or request.headers.get("X-Hub-Signature", "")
        if sig_header:
            expected = "sha256=" + hmac.new(webhook_secret.encode(), body_bytes, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(sig_header, expected):
                raise HTTPException(status_code=401, detail="Invalid signature")
        
        gitlab_token = request.headers.get("X-Gitlab-Token", "")
        if gitlab_token and not hmac.compare_digest(gitlab_token, webhook_secret):
            raise HTTPException(status_code=401, detail="Invalid GitLab token")

    payload = await request.json()
    ref = payload.get("ref", "")
    repo = (payload.get("repository") or payload.get("project") or {})
    repo_url = repo.get("clone_url") or repo.get("http_url") or repo.get("web_url", "")

    if ref and ref not in ("refs/heads/main", "refs/heads/master", ""):
        return {"ok": True, "skipped": f"branch {ref} ignored"}

    project_path = ""
    async with async_db(tenant_id=target_tenant) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT repo_url FROM projects WHERE id = %s AND tenant_id = %s", (project_id, target_tenant))
            row = await cur.fetchone()
            project_path = row.get("repo_url") if row else env_get("AGENT_WORKSPACE", default="")

    if not project_path:
        raise HTTPException(status_code=400, detail="project_path unknown")

    pipeline_id = f"INGEST-GIT-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()}"
    async with async_db(tenant_id=target_tenant) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO ingest_pipelines
                    (id, project_id, tenant_id, project_path, deep, status, requested_at)
                VALUES (%s, %s, %s, %s, %s, 'queued', NOW())
                """,
                (pipeline_id, project_id, target_tenant, project_path, True),
            )
            await conn.commit()

    ingest_body = IngestRequest(
        project_id=project_id,
        project_path=project_path,
        repo_url=repo_url,
        branch=ref.replace("refs/heads/", ""),
        deep=True
    )
    background_tasks.add_task(_run_ingest_pipeline, pipeline_id, project_id, target_tenant, ingest_body)
    
    await broadcast("ingest_queued", {"pipeline_id": pipeline_id, "trigger": "git_push"}, tenant_id=target_tenant)
    return {"ok": True, "pipeline_id": pipeline_id}
