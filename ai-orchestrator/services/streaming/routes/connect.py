from services.streaming.core.config import env_get
"""
streaming/routes/connect.py
============================
FastAPI Router for GitHub and external integrations.
"""
import json
import logging
import os
import asyncio
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, Response, BackgroundTasks
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["connect"])

# ── Models ───────────────────────────────────────────────────────────────────

class GitHubConnectRequest(BaseModel):
    repo_url: str
    access_token: Optional[str] = ""
    project_id: Optional[str] = ""
    branch: Optional[str] = ""
    webhook_secret: Optional[str] = ""

# ── Lazy-load helper ──────────────────────────────────────────────────────────

_gc = None

def _get_gh_connector():
    """Lazy-load github_connector module."""
    global _gc
    if _gc is not None:
        return _gc
    try:
        import sys
        from pathlib import Path
        root = str(Path(__file__).parent.parent.parent)
        if root not in sys.path:
            sys.path.insert(0, root)
        from services.github_connector import GitHubConnector
        _gc = GitHubConnector()
    except Exception as exc:
        log.debug("github_connector_unavailable error=%s", exc)
        _gc = None
    return _gc

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/connect/github", status_code=status.HTTP_202_ACCEPTED)
async def connect_github(
    body: GitHubConnectRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Start an async job to clone + analyse a GitHub repo."""
    gc = _get_gh_connector()
    if not gc:
        raise HTTPException(status_code=503, detail="github_connector module unavailable")
    
    # Run gc.connect in thread (it's likely sync/expensive)
    result = await asyncio.to_thread(
        gc.connect, 
        body.repo_url, body.access_token, body.project_id,
        tenant_id, body.branch, body.webhook_secret
    )
    return result

@router.get("/connect/jobs/{job_id}")
async def connect_job_status(job_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Get status and progress steps of a connect job."""
    gc = _get_gh_connector()
    if not gc:
        raise HTTPException(status_code=503, detail="github_connector module unavailable")
    
    job = await asyncio.to_thread(gc.get_job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job

@router.get("/connect/jobs/{job_id}/stream")
async def connect_job_stream(job_id: str, tenant_id: str = Depends(get_tenant_id)):
    """SSE stream — real-time progress for a connect job."""
    gc = _get_gh_connector()
    if not gc:
        raise HTTPException(status_code=503, detail="github_connector module unavailable")

    async def _event_generator():
        last_step = None
        for _ in range(180):
            job = await asyncio.to_thread(gc.get_job, job_id)
            if not job:
                yield "data: " + json.dumps({"error": "job not found"}) + "\n\n"
                return
            
            step = job.get("step")
            status = job.get("status")
            
            if step != last_step:
                last_step = step
                yield "data: " + json.dumps({
                    "step":       step,
                    "status":     status,
                    "steps_done": job.get("steps_done", []),
                    "steps_log":  job.get("steps_log",  {}),
                }) + "\n\n"
            
            if status in ("done", "error"):
                yield "data: " + json.dumps({
                    "status": status,
                    "result": job.get("result"),
                    "error":  job.get("error"),
                }) + "\n\n"
                return
            
            await asyncio.sleep(1)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )

@router.post("/connect/sync/{project_id}", status_code=status.HTTP_202_ACCEPTED)
async def connect_sync(project_id: str, tenant_id: str = Depends(get_tenant_id)):
    """Re-sync (git pull + re-analyse) an already-connected repository."""
    gc = _get_gh_connector()
    if not gc:
        raise HTTPException(status_code=503, detail="github_connector module unavailable")
    
    result = await asyncio.to_thread(gc.sync, project_id, tenant_id)
    return result

@router.get("/connect/repos")
async def connect_list_repos(tenant_id: str = Depends(get_tenant_id)):
    """List all connected repositories for the current tenant."""
    gc = _get_gh_connector()
    if not gc:
        raise HTTPException(status_code=503, detail="github_connector module unavailable")
    
    repos = await asyncio.to_thread(gc.list_repos, tenant_id)
    return {"repos": repos, "count": len(repos)}

@router.post("/webhooks/github")
async def webhook_github(request: Request):
    """GitHub webhook receiver — PR risk check."""
    raw_body = await request.body()
    headers = request.headers
    sig = headers.get("X-Hub-Signature-256", "")
    event = headers.get("X-GitHub-Event", "")

    webhook_secret = env_get("GITHUB_WEBHOOK_SECRET", default="")

    # Signature validation
    if webhook_secret:
        import hmac
        import hashlib
        # Constant-time comparison to prevent timing attacks
        expected = "sha256=" + hmac.new(webhook_secret.encode(), raw_body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(status_code=401, detail="invalid signature")

    try:
        payload = await request.json()
    except:
        payload = {}

    repo_full = payload.get("repository", {}).get("full_name", "")
    pr_number = payload.get("pull_request", {}).get("number")
    head_sha  = payload.get("pull_request", {}).get("head", {}).get("sha", "")

    # Record webhook event (background task might be better, but doing it here for status persistence)
    async with async_db(tenant_id="public") as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                INSERT INTO webhook_events
                    (tenant_id, event_type, repo_full_name, pr_number, head_sha, payload)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                ("public", event, repo_full, pr_number, head_sha, json.dumps(payload)),
            )
            await conn.commit()

    if event != "pull_request":
        return {"ok": True, "skipped": f"event={event}"}

    # Business logic for PR risk assessment
    gc = _get_gh_connector()
    access_token = env_get("GITHUB_TOKEN", default="")
    project_id = ""
    project_path = ""
    tenant_id = "public"

    async with async_db(tenant_id="public") as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT project_id, tenant_id, clone_path
                FROM connected_repos WHERE repo_url ILIKE %s LIMIT 1
                """,
                (f"%{repo_full}%",),
            )
            row = await cur.fetchone()
            if row:
                project_id = row["project_id"]
                tenant_id = row["tenant_id"]
                project_path = row.get("clone_path", "") or ""

    result: Dict[str, Any] = {}
    if gc:
        result = await asyncio.to_thread(
            gc.handle_pr_event,
            payload, access_token, project_id, project_path, tenant_id
        )
        # Update event with risk assessment
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE webhook_events
                    SET risk_score = %s, risk_label = %s,
                        recommendation = %s, result = %s
                    WHERE repo_full_name = %s AND pr_number = %s
                      AND received_at >= NOW() - INTERVAL '30 seconds'
                    """,
                    (
                        result.get("risk_score"), result.get("risk_label"),
                        result.get("recommendation"), json.dumps(result),
                        repo_full, pr_number,
                    ),
                )
                await conn.commit()

    return {"ok": True, "event": event, **result}
