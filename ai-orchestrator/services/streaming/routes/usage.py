"""
streaming/routes/usage.py
=========================
FastAPI Router for Usage and Billing operations.
"""
import logging
from typing import List, Optional, Dict, Any
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["usage"])

# Per-model pricing (USD per 1 000 tokens — input, output)
_MODEL_PRICE: Dict[str, tuple] = {
    "claude-opus-4-6":           (0.015,   0.075),
    "claude-sonnet-4-6":         (0.003,   0.015),
    "claude-haiku-4-5-20251001": (0.00025, 0.00125),
}

# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/usage")
async def get_usage(
    target_tenant: Optional[str] = Query(None, alias="tenant_id"),
    project_id: str = "",
    since: str = "",
    tenant_id: str = Depends(get_tenant_id)
):
    """Usage summary by tier."""
    tid = target_tenant or tenant_id
    filters: List[str] = ["tenant_id = %s"]
    params:  List      = [tid]
    
    if project_id:
        filters.append("project_id = %s")
        params.append(project_id)
    if since:
        filters.append("created_at >= %s")
        params.append(since)
    
    where = " AND ".join(filters)

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT tier,
                       COUNT(*)                    AS requests,
                       COALESCE(SUM(tokens_in),0)  AS tokens_in,
                       COALESCE(SUM(tokens_out),0) AS tokens_out,
                       COALESCE(SUM(cost_usd),0)   AS cost_usd,
                       ROUND(AVG(latency_ms))      AS avg_latency_ms
                FROM usage_log WHERE {where}
                GROUP BY tier ORDER BY tier
                """,
                params,
            )
            by_tier = await cur.fetchall()

            await cur.execute(
                f"""
                SELECT COUNT(*) AS total_requests,
                       COALESCE(SUM(cost_usd),0) AS total_cost_usd
                FROM usage_log WHERE {where}
                """,
                params,
            )
            totals = await cur.fetchone() or {}

    return {
        "tenant_id":      tid,
        "total_requests": totals.get("total_requests", 0),
        "total_cost_usd": float(totals.get("total_cost_usd", 0)),
        "by_tier":        by_tier,
    }

@router.get("/usage/billing")
async def get_usage_billing(
    project_id: str = "",
    since: str = "",
    tenant_id: str = Depends(get_tenant_id)
):
    """Billing breakdown: token costs per model."""
    filters: List[str] = ["tenant_id = %s"]
    params:  List      = [tenant_id]
    
    if project_id:
        filters.append("project_id = %s")
        params.append(project_id)
    if since:
        filters.append("created_at >= %s")
        params.append(since)
    
    where = " AND ".join(filters)

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute(
                f"""
                SELECT model,
                       COUNT(*)                    AS requests,
                       COALESCE(SUM(tokens_in),0)  AS tokens_in,
                       COALESCE(SUM(tokens_out),0) AS tokens_out,
                       COALESCE(SUM(cost_usd),0)   AS recorded_cost_usd
                FROM usage_log WHERE {where}
                GROUP BY model ORDER BY tokens_in + tokens_out DESC
                """,
                params,
            )
            rows = await cur.fetchall()

    line_items = []
    total_cost = 0.0
    for r in rows:
        model = r["model"] or "unknown"
        price = _MODEL_PRICE.get(model)
        if price:
            calc_cost = (r["tokens_in"]  / 1000 * price[0] +
                         r["tokens_out"] / 1000 * price[1])
            src = "standard"
        else:
            calc_cost = float(r["recorded_cost_usd"])
            src = "recorded"
        total_cost += calc_cost
        line_items.append({
            "model":       model,
            "requests":    r["requests"],
            "tokens_in":   r["tokens_in"],
            "tokens_out":  r["tokens_out"],
            "cost_usd":    round(calc_cost, 6),
            "pricing_src": src,
        })

    return {
        "tenant_id":      tenant_id,
        "total_cost_usd": round(total_cost, 6),
        "line_items":     line_items,
        "currency":       "USD",
    }
