"""
opencode.py
===========
Dashboard API routes for the OpenCode coding assistant integration.

Endpoints:
  GET  /api/v5/opencode/health          — serve mode vs subprocess health
  GET  /api/v5/opencode/sessions        — list active sessions
  POST /api/v5/opencode/run             — one-shot coding task (non-streaming)
  GET  /api/v5/opencode/sessions/{id}   — session detail
  POST /api/v5/opencode/sessions/{id}/message  — send message to session
  DELETE /api/v5/opencode/sessions/{id} — close session
"""

from __future__ import annotations

import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse

log = logging.getLogger("opencode-routes")
router = APIRouter(prefix="/api/v5/opencode", tags=["opencode"])


def _client():
    from services.opencode_client import get_opencode_client
    return get_opencode_client()


# ── Health ──────────────────────────────────────────────────────────────────────

@router.get("/health")
async def opencode_health(request: Request):
    try:
        status = await _client().health()
        return JSONResponse(status)
    except Exception as e:
        return JSONResponse({"mode": "error", "status": "error", "error": str(e)}, status_code=500)


# ── Sessions ────────────────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions(request: Request):
    try:
        sessions = await _client().list_sessions()
        return JSONResponse({"sessions": sessions, "count": len(sessions)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, request: Request):
    client = _client()
    session = client._sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    state = await client.get_session_state(session)
    return JSONResponse(state)


@router.post("/sessions")
async def create_session(request: Request):
    body = await request.json()
    task_id = body.get("task_id", "manual")
    tenant_id = body.get("tenant_id", "local")
    provider = body.get("provider", "anthropic")
    model = body.get("model", "claude-sonnet-4-6")
    workspace = body.get("workspace", "/workspace")
    context_files = body.get("context_files", [])

    try:
        session = await _client().new_session(
            task_id=task_id,
            tenant_id=tenant_id,
            context_files=context_files,
            provider=provider,
            model=model,
            workspace=workspace,
        )
        return JSONResponse({"session_id": session.session_id, "status": "active"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/sessions/{session_id}/message")
async def send_session_message(session_id: str, request: Request):
    """Send a message and stream the response via SSE."""
    body = await request.json()
    message = body.get("message", "")
    attachments = body.get("attachments", [])

    client = _client()
    session = client._sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    async def _sse_stream() -> AsyncGenerator[str, None]:
        try:
            async for chunk in client.send_message(session, message, attachments=attachments):
                payload = json.dumps({"token": chunk})
                yield f"data: {payload}\n\n"
            state = await client.get_session_state(session)
            yield f"data: {json.dumps({'done': True, 'files_modified': state.get('files_modified', [])})}\n\n"
            yield "data: [DONE]\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        _sse_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.delete("/sessions/{session_id}")
async def close_session(session_id: str, request: Request):
    client = _client()
    session = client._sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    result = await client.close_session(session)
    return JSONResponse({
        "session_id": result.session_id,
        "summary": result.summary,
        "files_modified": result.files_modified,
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "cost_usd": result.cost_usd,
        "success": result.success,
    })


# ── One-Shot ────────────────────────────────────────────────────────────────────

@router.post("/run")
async def run_oneshot(request: Request):
    """Execute a one-shot coding task. Returns full result (non-streaming)."""
    body = await request.json()
    prompt = body.get("prompt", "")
    if not prompt:
        return JSONResponse({"error": "prompt is required"}, status_code=400)

    task_id = body.get("task_id", "api-oneshot")
    tenant_id = body.get("tenant_id", "local")
    provider = body.get("provider", "anthropic")
    model = body.get("model", "claude-sonnet-4-6")
    workspace = body.get("workspace", "/workspace")
    context_files = body.get("context_files", [])

    try:
        result = await _client().run_oneshot(
            prompt=prompt,
            task_id=task_id,
            tenant_id=tenant_id,
            provider=provider,
            model=model,
            context_files=context_files,
            workspace=workspace,
        )
        return JSONResponse({
            "session_id": result.session_id,
            "summary": result.summary,
            "files_modified": result.files_modified,
            "diff": result.diff,
            "tokens_in": result.tokens_in,
            "tokens_out": result.tokens_out,
            "cost_usd": result.cost_usd,
            "backend_used": result.backend_used,
            "success": result.success,
            "error": result.error,
        })
    except Exception as e:
        log.exception("opencode oneshot failed")
        return JSONResponse({"error": str(e), "success": False}, status_code=500)
