"""
streaming/routes/simulate.py
============================
FastAPI Router for Simulation operations.
"""
import logging
import asyncio
import os
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db
from services.streaming.core.security_config import safe_project_path

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["simulate"])

# ── Models ───────────────────────────────────────────────────────────────────

class ChangeSimulationRequest(BaseModel):
    change_spec: Dict[str, Any]
    project_path: str
    project_id: str = ""

class TaskSimulationRequest(BaseModel):
    project_path: str
    project_id: str = ""

class PlanSimulationRequest(BaseModel):
    tasks: List[Dict[str, Any]]
    project_path: str
    project_id: str = ""

class BlastRadiusRequest(BaseModel):
    files: List[str]
    project_path: str
    project_id: str = ""
    max_depth: int = 4

# ── Lazy-load helper ──────────────────────────────────────────────────────────

_tm = None

def _get_time_machine():
    """Lazy-load time_machine module."""
    global _tm
    if _tm is not None:
        return _tm
    try:
        from ...time_machine import TimeMachine
        _tm = TimeMachine()
    except Exception as exc:
        log.debug("time_machine_unavailable error=%s", exc)
        _tm = None
    return _tm

# ── Routes ────────────────────────────────────────────────────────────────────

def _safe_sim_path(raw: str) -> str:
    """Validate project_path against AGENT_WORKSPACE; raises HTTPException on traversal."""
    try:
        return safe_project_path(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/simulate/change")
async def simulate_change(
    body: ChangeSimulationRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Simulate a proposed code change."""
    tm = _get_time_machine()
    if not tm:
        raise HTTPException(status_code=503, detail="time_machine module unavailable")
    project_path = _safe_sim_path(body.project_path)
    result = await asyncio.to_thread(tm.simulate_change, body.change_spec, project_path, body.project_id, tenant_id)
    return result

@router.post("/simulate/task/{task_id}")
async def simulate_task(
    task_id: str,
    body: TaskSimulationRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Simulate the impact of executing a task."""
    tm = _get_time_machine()
    if not tm:
        raise HTTPException(status_code=503, detail="time_machine module unavailable")
    project_path = _safe_sim_path(body.project_path)
    result = await asyncio.to_thread(tm.simulate_task, task_id, project_path, body.project_id, tenant_id)
    return result

@router.post("/simulate/plan")
async def simulate_plan(
    body: PlanSimulationRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Simulate an ordered task plan."""
    tm = _get_time_machine()
    if not tm:
        raise HTTPException(status_code=503, detail="time_machine module unavailable")
    project_path = _safe_sim_path(body.project_path)
    result = await asyncio.to_thread(tm.simulate_plan, body.tasks, project_path, body.project_id, tenant_id)
    return result

@router.post("/simulate/blast")
async def simulate_blast(
    body: BlastRadiusRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Compute blast radius for a set of files."""
    tm = _get_time_machine()
    if not tm:
        raise HTTPException(status_code=503, detail="time_machine module unavailable")
    project_path = _safe_sim_path(body.project_path)
    result = await asyncio.to_thread(tm.blast_radius, body.files, project_path, body.project_id, tenant_id, body.max_depth)
    return result

@router.get("/simulate/history")
async def simulate_history(
    project_id: str = "",
    limit: int = Query(50, ge=1, le=200),
    tenant_id: str = Depends(get_tenant_id)
):
    """List past simulation runs."""
    tm = _get_time_machine()
    if not tm:
        raise HTTPException(status_code=503, detail="time_machine module unavailable")
    
    history = await asyncio.to_thread(tm.simulation_history, project_id, tenant_id, limit)
    return {"simulations": history, "count": len(history)}
