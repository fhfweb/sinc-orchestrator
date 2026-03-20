"""
streaming/routes/entropy.py
===========================
FastAPI Router for Entropy (Uncertainty) operations.
"""
import logging
import asyncio
import os
from collections import Counter
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db
from services.streaming.core.security_config import safe_project_path

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["entropy"])

# ── Models ───────────────────────────────────────────────────────────────────

class EntropyScanRequest(BaseModel):
    project_path: str
    project_id: str = ""

class EntropyScanLocalRequest(BaseModel):
    path: str
    label: Optional[str] = None
    min_score: float = 0.0
    churn: Optional[Dict[str, float]] = None

class SeedTasksRequest(BaseModel):
    project_id: str
    threshold: float = 0.70

# ── Lazy-load helper ──────────────────────────────────────────────────────────

_scanner = None

def _get_scanner():
    """Lazy-load entropy_scanner module."""
    global _scanner
    if _scanner is not None:
        return _scanner
    try:
        from ...entropy_scanner import EntropyScanner
        _scanner = EntropyScanner()
    except Exception as exc:
        log.debug("entropy_scanner_unavailable error=%s", exc)
        _scanner = None
    return _scanner

def _pct(vals: list, p: float) -> float:
    if not vals:
        return 0.0
    sv  = sorted(vals)
    idx = min(int(len(sv) * p / 100), len(sv) - 1)
    return sv[idx]


def _execution_risk_profile(rows: list[dict]) -> tuple[str, list[str]]:
    if not rows:
        return "normal", []
    top = max(float(row.get("entropy_score") or 0.0) for row in rows)
    labels = {str(row.get("label") or "").strip().lower() for row in rows}
    recommendations = [
        "Prefer patch_file over broad rewrites on hot files.",
        "Run focused validation before broader regression.",
    ]
    if "structural_hazard" in labels or top >= 0.85:
        recommendations.append("Treat this edit as high-risk and avoid multi-file refactors in one pass.")
        return "extreme", recommendations
    if "critical" in labels or top >= 0.75:
        recommendations.append("Split the change into smaller steps and checkpoint after each edit.")
        return "guarded", recommendations
    if "refactor" in labels or top >= 0.65:
        recommendations.append("Review dependencies touched by the file before applying changes.")
        return "elevated", recommendations
    return "normal", recommendations

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/entropy/scan")
async def entropy_scan(
    body: EntropyScanRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Scan a project and store entropy scores to DB."""
    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")
    try:
        project_path = safe_project_path(body.project_path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result = await asyncio.to_thread(scanner.scan_and_store, project_path, body.project_id, tenant_id)
    return result

@router.get("/entropy/report")
async def entropy_report(
    project_id: str = "",
    label_filter: Optional[str] = Query(None, alias="label"),
    tenant_id: str = Depends(get_tenant_id)
):
    """Latest entropy report for a project."""
    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")
    
    rows = await asyncio.to_thread(scanner.latest_report, project_id, tenant_id)
    if label_filter:
        rows = [r for r in rows if r.get("label") == label_filter]
    rows.sort(key=lambda r: r.get("entropy_score", 0), reverse=True)
    return {"files": rows, "count": len(rows)}


@router.get("/entropy/risk-context")
async def entropy_risk_context(
    project_id: str = "",
    files: str = Query("", max_length=4000),
    limit: int = Query(5, ge=1, le=20),
    tenant_id: str = Depends(get_tenant_id),
):
    requested_files = [
        str(item or "").strip().replace("\\", "/")
        for item in files.split(",")
        if str(item or "").strip()
    ]
    if not requested_files:
        return {"profile": "normal", "files": [], "count": 0, "recommendations": []}

    async with async_db(bypass_rls=True) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                """
                SELECT project_id, tenant_id, file_path, entropy_score, label,
                       complexity, coupling, max_fn_lines, scan_at
                  FROM v_entropy_latest
                 WHERE tenant_id = %s
                   AND (%s = '' OR project_id = %s)
                 ORDER BY entropy_score DESC
                 LIMIT 1000
                """,
                (tenant_id, project_id, project_id),
            )
            rows = [dict(row) for row in await cur.fetchall()]

    matches = []
    seen = set()
    for row in rows:
        file_path = str(row.get("file_path") or "").strip().replace("\\", "/")
        if not file_path:
            continue
        for candidate in requested_files:
            if file_path == candidate or file_path.endswith(candidate):
                if file_path in seen:
                    break
                seen.add(file_path)
                row["match_kind"] = "exact" if file_path == candidate else "suffix"
                matches.append(row)
                break
        if len(matches) >= limit:
            break

    profile, recommendations = _execution_risk_profile(matches)
    return {
        "profile": profile,
        "files": matches,
        "count": len(matches),
        "recommendations": recommendations,
    }

@router.get("/entropy/trend")
async def entropy_trend(
    project_id: str = "",
    file_path: str = Query(..., alias="file"),
    tenant_id: str = Depends(get_tenant_id)
):
    """Per-file entropy trend over time."""
    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")
    
    history = await asyncio.to_thread(scanner.trend, project_id, tenant_id, file_path)
    return {"file": file_path, "history": history}

@router.get("/entropy/project-trend")
async def entropy_project_trend(
    project_id: str = "",
    tenant_id: str = Depends(get_tenant_id)
):
    """Aggregate project entropy trend."""
    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")
    
    trend = await asyncio.to_thread(scanner.project_trend, project_id, tenant_id)
    return {"project_id": project_id, "trend": trend}

@router.get("/entropy/velocity")
async def entropy_velocity_api(
    project_id: str = "",
    target_tenant: Optional[str] = Query(None, alias="tenant_id"),
    window: int = 5,
    tenant_id: str = Depends(get_tenant_id)
):
    """Entropy velocity (rate of change)."""
    tid = target_tenant or tenant_id
    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")
    return await asyncio.to_thread(scanner.entropy_velocity, project_id, tid, window=window)

@router.get("/entropy/file-velocity")
async def entropy_file_velocity_api(
    project_id: str = "",
    file_path: str = Query(..., alias="file"),
    window: int = 10,
    tenant_id: str = Depends(get_tenant_id)
):
    """Per-file entropy velocity."""
    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")
    return await asyncio.to_thread(scanner.file_velocity, project_id, tenant_id, file_path, window=window)

@router.post("/entropy/scan-local")
async def entropy_scan_local(
    body: EntropyScanLocalRequest
):
    """In-memory scan — no auth, no DB."""
    try:
        safe_path = safe_project_path(body.path)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not os.path.isdir(safe_path):
        raise HTTPException(status_code=400, detail=f"not a directory: {safe_path}")

    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")

    files = await asyncio.to_thread(scanner.scan_project, safe_path, churn_map=body.churn)

    if body.label:
        files = [f for f in files if f.get("label") == body.label]
    if body.min_score > 0.0:
        files = [f for f in files if f.get("entropy_score", 0) >= body.min_score]

    scores = [f["entropy_score"] for f in files if not f.get("is_test")]
    avg    = round(sum(scores) / len(scores), 4) if scores else 0.0
    label_counts = dict(Counter(f["label"] for f in files if not f.get("is_test")))

    return {
        "files":        files,
        "count":        len(files),
        "avg_entropy":  avg,
        "p50":          _pct(scores, 50),
        "p90":          _pct(scores, 90),
        "label_counts": label_counts,
    }

@router.post("/entropy/seed-tasks")
async def entropy_seed_tasks(
    body: SeedTasksRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Create tasks for high-entropy files."""
    scanner = _get_scanner()
    if not scanner:
        raise HTTPException(status_code=503, detail="entropy_scanner module unavailable")
    
    created = await asyncio.to_thread(scanner.seed_tasks, body.project_id, tenant_id, threshold=body.threshold)
    return {"created": created, "count": len(created)}

@router.get("/entropy/dashboard")
async def entropy_dashboard_api(
    project_id: str = "",
    tenant_id: str = Depends(get_tenant_id)
):
    """Entropy KPI dashboard summary."""
    scanner = _get_scanner()
    rows: list = []
    if scanner:
        rows = await asyncio.to_thread(scanner.latest_report, project_id, tenant_id, limit=500)
    
    rows.sort(key=lambda r: r.get("entropy_score", 0), reverse=True)
    src_rows = [r for r in rows if not r.get("is_test")]
    scores   = [r.get("entropy_score", 0) for r in src_rows]
    avg_e    = round(sum(scores) / len(scores), 3) if scores else 0.0

    label_counts = dict(Counter(r.get("label", "healthy") for r in src_rows))
    hotspots = sorted(
        [r for r in src_rows if r.get("hotspot_score", 0) > 0],
        key=lambda r: r.get("hotspot_score", 0), reverse=True
    )[:10]

    return {
        "project_id":   project_id,
        "total_files":  len(src_rows),
        "avg_entropy":  avg_e,
        "p50":          _pct(scores, 50),
        "p90":          _pct(scores, 90),
        "label_counts": label_counts,
        "hotspots":     hotspots,
        "top_files":    rows[:20],
    }
