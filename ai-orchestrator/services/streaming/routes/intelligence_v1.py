"""
intelligence_v1.py
==================
V1 Intelligence & Governance Observability Endpoints.
Provides insights into Admission Control and Strategic Planning.
"""
from fastapi import APIRouter, Depends, HTTPException
from typing import Any, Dict
import logging

from ..core.auth import get_tenant_id
from services.cognitive_orchestrator import get_orchestrator

log = logging.getLogger("orch.intel_v1")

router = APIRouter(prefix="/api/v1", tags=["Intelligence V1"])

@router.get("/admission/stats")
async def get_admission_stats(tenant_id: str = Depends(get_tenant_id)):
    """
    Returns real-time admission control statistics.
    """
    orch = get_orchestrator()
    adm = orch.registry.get("admission")
    if not adm:
        return {
            "status": "unavailable",
            "reason": "AdmissionController registry miss"
        }
    
    # In a real scenario, we might fetch from Redis or internal metrics
    # For now, we return a snapshot of the controller's state if available
    return {
        "status": "active",
        "tenant_id": tenant_id,
        "concurrency_limit": getattr(adm, "_max_concurrency", 5),
        "cost_limit": getattr(adm, "_max_cost_usd", 10.0),
        "metrics": {
            "admitted_total": 0, # Placeholders for now, would be synced with Redis
            "deferred_total": 0,
            "rejected_total": 0
        }
    }

@router.get("/strategic/performance")
async def get_strategic_performance(tenant_id: str = Depends(get_tenant_id)):
    """
    Returns Strategic Planner performance metrics (Latency, Savings, Bypass).
    """
    orch = get_orchestrator()
    stats = orch.get_stats()
    
    # Enhance stats with Strategic Planner specific data
    health = await orch.registry.check_health()
    
    planner_stats = {
        "orchestrator_snapshot": stats,
        "planner_type": "MCTS (Pillar III)",
        "reward_factors": ["success_rate", "latency_penalty", "token_cost_penalty"],
        "governance_health": health
    }
    
    return planner_stats

@router.get("/health")
async def get_cognitive_health():
    """Returns a full health report of the cognitive infrastructure."""
    orch = get_orchestrator()
    return await orch.registry.check_health()
