from services.streaming.core.config import env_get
"""
streaming/routes/system.py
==========================
FastAPI Router for System diagnostics, health, and metadata.
"""
import logging
import asyncio
import uuid
from typing import List, Optional, Dict, Any
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status, Response
from fastapi.responses import HTMLResponse, FileResponse
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant, now_iso
from services.streaming.core.db import async_db
from services.streaming.core.billing import PLAN_FEATURES, SSE_LIMITS
from services.streaming.core.state_plane import get_system_status_snapshot
from services.streaming.core.runtime_plane import (
    compute_readiness_snapshot,
    get_latest_readiness_snapshot,
    list_incidents,
    observer_tick_once,
    reconcile_incidents,
    readiness_tick_once,
    scheduler_tick_once,
)
from services.streaming.core.external_agent_bridge import (
    external_bridge_tick_once,
    get_external_bridge_status,
)
from services.streaming.core.governance_plane import (
    deploy_verify_tick_once,
    finops_tick_once,
    get_latest_governance_snapshot,
    mutation_tick_once,
    pattern_promotion_tick_once,
    policy_tick_once,
    release_tick_once,
)
from services.otel_setup import span, force_flush_otel

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["system"])

_SDK_DIR = Path(__file__).resolve().parents[3] / "sdk"

# ── API discovery ─────────────────────────────────────────────────────────────

@router.get("/")
async def api_info():
    return {
        "service": "SINC Orchestrator API",
        "version": "5.0",
        "openapi": "/openapi.json",
        "endpoints": {
            "health":        "GET /health",
            "events":        "GET /events  (SSE)",
            "tasks":         "GET|POST /tasks",
            "agents":        "GET /agents",
            "projects":      "GET|POST /projects",
            "ask":           "POST /ask",
            "ask_stream":    "GET /ask/stream?prompt=...",
            "ingest":        "POST /ingest",
            "usage":         "GET /usage",
            "gates":         "GET /gates",
            "admin":         "GET|POST /admin/tenants",
        },
        "ts": now_iso(),
    }

# ── System status ─────────────────────────────────────────────────────────────

@router.get("/status")
async def system_status(tenant_id: str = Depends(get_tenant_id)):
    """One-call system snapshot."""
    return await get_system_status_snapshot(tenant_id)


@router.get("/readiness")
async def readiness_status(tenant_id: str = Depends(get_tenant_id)):
    """Latest persisted readiness snapshot for the Python runtime."""
    return await get_latest_readiness_snapshot(tenant_id)


@router.get("/readiness/live")
async def readiness_status_live(tenant_id: str = Depends(get_tenant_id)):
    """Compute a fresh readiness snapshot directly from Postgres."""
    return await compute_readiness_snapshot(tenant_id)


@router.get("/incidents")
async def incident_list(
    limit: int = Query(50, ge=1, le=200),
    status_filter: str = Query("all", pattern=r"^(all|open|resolved)$"),
    tenant_id: str = Depends(get_tenant_id),
):
    """Latest incidents recorded by the Python observer/runtime."""
    incidents = await list_incidents(tenant_id=tenant_id, limit=limit, status_filter=status_filter)
    return {"incidents": incidents, "count": len(incidents), "status_filter": status_filter, "source": "db"}


@router.post("/incidents/reconcile")
async def incident_reconcile(
    tenant_id: str = Depends(get_tenant_id),
):
    readiness = await compute_readiness_snapshot(tenant_id=tenant_id)
    result = await reconcile_incidents(tenant_id=tenant_id, readiness=readiness)
    latest = await compute_readiness_snapshot(tenant_id=tenant_id)
    await readiness_tick_once(tenant_id=tenant_id)
    return {
        "ok": True,
        "resolved": result["resolved"],
        "readiness": latest,
        "ts": now_iso(),
    }


@router.get("/external-bridge/status")
async def external_bridge_status(tenant_id: str = Depends(get_tenant_id)):
    return await get_external_bridge_status(tenant_id=tenant_id)


@router.post("/otel/probe")
async def otel_probe(request: Request, tenant_id: str = Depends(get_tenant_id)):
    probe_id = f"probe-{uuid.uuid4().hex[:12]}"
    trace_id = getattr(request.state, "trace_id", "")
    with span("system.otel_probe", tenant_id=tenant_id, probe_id=probe_id, trace_id=trace_id):
        log.info("otel_probe_emitted tenant=%s probe_id=%s trace_id=%s", tenant_id, probe_id, trace_id)
    flushed = force_flush_otel()
    return {
        "ok": True,
        "probe_id": probe_id,
        "trace_id": trace_id,
        "flushed": flushed,
        "ts": now_iso(),
    }

# ── Prometheus metrics ────────────────────────────────────────────────────────

@router.get("/metrics")
async def prometheus_metrics():
    """Prometheus text-format metrics."""
    try:
        from services.metrics_exporter import generate_metrics
        return Response(content=generate_metrics(), media_type="text/plain")
    except ImportError:
        pass

    lines = [
        "# HELP orchestrator_up 1 if the service is running",
        "# TYPE orchestrator_up gauge",
        "orchestrator_up 1",
    ]

    # ── Active tasks by status ────────────────────────────────────────────────
    try:
        async with async_db(tenant_id="public") as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT status, COUNT(*) AS n FROM tasks GROUP BY status"
                )
                rows = await cur.fetchall()
                lines.append("# HELP orchestrator_tasks_total Tasks grouped by status")
                lines.append("# TYPE orchestrator_tasks_total gauge")
                for row in rows:
                    lines.append(
                        f'orchestrator_tasks_total{{status="{row["status"]}"}} {row["n"]}'
                    )
    except Exception:
        pass

    # ── Live SSE connections ───────────────────────────────────────────────────
    try:
        from services.streaming.core.redis_ import get_async_redis
        r = get_async_redis()
        if r:
            keys = await r.keys("sse_conns:*")
            total_sse = 0
            lines.append("# HELP orchestrator_sse_connections Live SSE connections per tenant")
            lines.append("# TYPE orchestrator_sse_connections gauge")
            for key in keys:
                tenant = key.split("sse_conns:", 1)[-1]
                count  = await r.scard(key)
                total_sse += count
                lines.append(f'orchestrator_sse_connections{{tenant="{tenant}"}} {count}')
            lines.append(f"orchestrator_sse_connections_total {total_sse}")
    except Exception:
        pass

    # ── Circuit breaker states ────────────────────────────────────────────────
    try:
        from services.streaming.core.circuit import _breakers
        lines.append("# HELP orchestrator_circuit_breaker_open 1 if circuit is OPEN")
        lines.append("# TYPE orchestrator_circuit_breaker_open gauge")
        for name, breaker in _breakers.items():
            is_open = 1 if breaker.state.value == "open" else 0
            lines.append(f'orchestrator_circuit_breaker_open{{name="{name}"}} {is_open}')
            lines.append(
                f'orchestrator_circuit_breaker_failures{{name="{name}"}} {breaker.failure_count}'
            )
    except Exception:
        pass

    # ── Ask cache hits per tenant ─────────────────────────────────────────────
    try:
        from services.streaming.core.redis_ import get_async_redis
        _r = get_async_redis()
        if _r:
            hit_keys = await _r.keys("ask_cache_hits:*")
            lines.append("# HELP orchestrator_ask_cache_hits_total Ask responses served from cache")
            lines.append("# TYPE orchestrator_ask_cache_hits_total counter")
            for key in hit_keys:
                tenant = key.split("ask_cache_hits:", 1)[-1]
                count  = int(await _r.get(key) or 0)
                lines.append(f'orchestrator_ask_cache_hits_total{{tenant="{tenant}"}} {count}')
    except Exception:
        pass

    return Response(content="\n".join(lines) + "\n", media_type="text/plain")

# ── Service catalog ───────────────────────────────────────────────────────────

@router.get("/catalog")
async def service_catalog():
    """Public service catalog — available plans, models, and limits."""
    import os
    return {
        "plans": {
            "free": {
                "requests_per_minute": 10,
                "tokens_per_day": 50000,
                "sse_connections": SSE_LIMITS["free"],
                "backends": PLAN_FEATURES["free"]["backends"],
            },
            "pro": {
                "requests_per_minute": 120,
                "tokens_per_day": 1000000,
                "sse_connections": SSE_LIMITS["pro"],
                "backends": PLAN_FEATURES["pro"]["backends"],
            }
        },
        "models": {
            "ollama": {
                "general": env_get("OLLAMA_MODEL_GENERAL", default="qwen2.5:7b-instruct-q4_K_M"),
            },
            "anthropic": {"available": bool(env_get("ANTHROPIC_API_KEY"))},
        },
        "version": "5.0",
        "ts": now_iso(),
    }

# ── Tenant audit log ──────────────────────────────────────────────────────────

@router.get("/audit")
async def tenant_audit_log(
    action: Optional[str] = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = 0,
    tenant_id: str = Depends(get_tenant_id)
):
    """Tenant-scoped audit trail."""
    clauses: List[str] = ["(target_id = %s OR metadata->>'tenant_id' = %s)"]
    params:  List      = [tenant_id, tenant_id]
    if action:
        clauses.append("action = %s")
        params.append(action)
    where = " AND ".join(clauses)

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"SELECT action, actor, target_type, target_id, created_at "
                f"FROM audit_log WHERE {where} "
                f"ORDER BY created_at DESC LIMIT %s OFFSET %s",
                params + [limit, offset],
            )
            rows = await cur.fetchall()
    return {"records": rows, "count": len(rows), "offset": offset}

# ── SDK distribution ──────────────────────────────────────────────────────────

@router.get("/sdk/{asset_path:path}")
async def serve_sdk(asset_path: str):
    """Serve a file from the SDK directory, including nested assets."""
    candidate = (_SDK_DIR / asset_path).resolve()
    sdk_root = _SDK_DIR.resolve()
    try:
        candidate.relative_to(sdk_root)
    except ValueError:
        raise HTTPException(status_code=404, detail="not found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(path=candidate)

@router.get("/sdk")
async def list_sdk():
    """List available SDK files."""
    if not _SDK_DIR.exists():
        return {"files": []}
    files = [
        str(f.relative_to(_SDK_DIR)).replace("\\", "/")
        for f in sorted(_SDK_DIR.rglob("*"))
        if f.is_file()
    ]
    return {"files": files}

# ── Scheduler trigger ─────────────────────────────────────────────────────────

@router.post("/scheduler/run")
async def scheduler_run(tenant_id: str = Depends(get_tenant_id)):
    """Trigger immediate scheduler tick in the Python runtime."""
    result = await scheduler_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.post("/observer/run")
async def observer_run(tenant_id: str = Depends(get_tenant_id)):
    """Trigger immediate observer tick in the Python runtime."""
    result = await observer_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.post("/readiness/run")
async def readiness_run(tenant_id: str = Depends(get_tenant_id)):
    """Trigger immediate readiness tick in the Python runtime."""
    result = await readiness_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.post("/external-bridge/run")
async def external_bridge_run(tenant_id: str = Depends(get_tenant_id)):
    """Trigger immediate external-agent bridge tick in the Python runtime."""
    result = await external_bridge_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.post("/policy/run")
async def policy_run(tenant_id: str = Depends(get_tenant_id)):
    result = await policy_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.get("/policy")
async def policy_status(tenant_id: str = Depends(get_tenant_id)):
    return await get_latest_governance_snapshot("policy", tenant_id=tenant_id)


@router.post("/mutation/run")
async def mutation_run(tenant_id: str = Depends(get_tenant_id)):
    result = await mutation_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.get("/mutation")
async def mutation_status(tenant_id: str = Depends(get_tenant_id)):
    return await get_latest_governance_snapshot("mutation", tenant_id=tenant_id)


@router.post("/finops/run")
async def finops_run(tenant_id: str = Depends(get_tenant_id)):
    result = await finops_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.get("/finops")
async def finops_status(tenant_id: str = Depends(get_tenant_id)):
    return await get_latest_governance_snapshot("finops", tenant_id=tenant_id)


@router.post("/deploy-verify/run")
async def deploy_verify_run(tenant_id: str = Depends(get_tenant_id)):
    result = await deploy_verify_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.get("/deploy-verify")
async def deploy_verify_status(tenant_id: str = Depends(get_tenant_id)):
    return await get_latest_governance_snapshot("deploy", tenant_id=tenant_id)


@router.post("/pattern-promotion/run")
async def pattern_promotion_run(tenant_id: str = Depends(get_tenant_id)):
    result = await pattern_promotion_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.get("/pattern-promotion")
async def pattern_promotion_status(tenant_id: str = Depends(get_tenant_id)):
    return await get_latest_governance_snapshot("pattern_promotion", tenant_id=tenant_id)


@router.post("/release/run")
async def release_run(tenant_id: str = Depends(get_tenant_id)):
    result = await release_tick_once(tenant_id=tenant_id)
    return {"ok": result.get("status") == "ok", **result}


@router.get("/release")
async def release_status(tenant_id: str = Depends(get_tenant_id)):
    return await get_latest_governance_snapshot("release", tenant_id=tenant_id)
