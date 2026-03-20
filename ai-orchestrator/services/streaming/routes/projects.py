"""
streaming/routes/projects.py
============================
FastAPI Router for Project operations.
"""
import logging
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db
from services.streaming.core.billing import PLAN_FEATURES

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["projects"])

@router.get("/projects")
async def list_projects(
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=100),
    tenant_id: str = Depends(get_tenant_id)
):
    offset = (page - 1) * per_page
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT id, name, repo_url, stack, status, created_at, updated_at
                FROM projects WHERE tenant_id = %s
                ORDER BY created_at DESC LIMIT %s OFFSET %s
            """, (tenant_id, per_page, offset))
            rows = await cur.fetchall()
            
            await cur.execute("SELECT COUNT(*) FROM projects WHERE tenant_id = %s", (tenant_id,))
            count_row = await cur.fetchone()
            total = count_row["count"] if count_row else 0
            
    return {"projects": rows, "total": total, "page": page, "per_page": per_page}

@router.post("/projects", status_code=status.HTTP_201_CREATED)
async def create_project(
    body: Dict[str, Any],
    tenant_id: str = Depends(get_tenant_id),
    tenant: dict = Depends(get_tenant)
):
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
        
    pid = body.get("id") or name.lower().replace(" ", "-")
    repo_url = body.get("repo_url", "")
    stack = body.get("stack", "")
    
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            plan = tenant.get("plan", "free")
            max_p = PLAN_FEATURES.get(plan, PLAN_FEATURES["free"])["max_projects"]
            if max_p > 0:
                await cur.execute("SELECT COUNT(*) FROM projects WHERE tenant_id = %s AND status != 'archived'", (tenant_id,))
                cnt = await cur.fetchone()
                if cnt and cnt["count"] >= max_p:
                    raise HTTPException(status_code=429, detail=f"Project limit reached for plan '{plan}'")

            await cur.execute("""
                INSERT INTO projects (id, tenant_id, name, repo_url, stack, status)
                VALUES (%s, %s, %s, %s, %s, 'active')
                ON CONFLICT (id) DO UPDATE
                    SET name = EXCLUDED.name, repo_url = EXCLUDED.repo_url,
                        stack = EXCLUDED.stack, updated_at = NOW()
                RETURNING id, name, repo_url, status, created_at
            """, (pid, tenant_id, name, repo_url, stack))
            row = await cur.fetchone()
            await conn.commit()
            
    return row

@router.get("/projects/{project_id}")
async def get_project(project_id: str, tenant_id: str = Depends(get_tenant_id)):
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT * FROM projects WHERE id = %s AND tenant_id = %s", (project_id, tenant_id))
            row = await cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Project not found")
    return row

@router.delete("/projects/{project_id}")
async def delete_project(project_id: str, tenant_id: str = Depends(get_tenant_id)):
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            await cur.execute("UPDATE projects SET status = 'archived' WHERE id = %s AND tenant_id = %s RETURNING id", (project_id, tenant_id))
            if not await cur.fetchone():
                raise HTTPException(status_code=404, detail="Project not found")
            await conn.commit()
    return {"ok": True}
