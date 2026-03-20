from services.streaming.core.config import env_get
"""
streaming/routes/plans.py
=========================
POST /plan                      — decompose a goal into tasks via LLM
GET  /plans                     — list plans for this tenant
GET  /plans/<plan_id>/graph     — task DAG as nodes + directed edges
"""
import json
import logging
import os
import re
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel

from services.http_client import create_resilient_client
from services.streaming.core.auth import get_tenant_id, get_tenant
from services.streaming.core.db import async_db
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["plans"])
from services.mcts_planner import MCTSPlanner
mcts_planner = MCTSPlanner()

class PlanCreate(BaseModel):
    goal: str
    project_id: str = ""
    agent: str = ""
    context: str = ""


# ── POST /plan ────────────────────────────────────────────────────────────────

@router.post("/plan", status_code=status.HTTP_201_CREATED)
async def create_plan(
    body: PlanCreate,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Decompose a high-level goal into tasks with dependencies via Anthropic.
    """
    goal       = body.goal.strip()
    project_id = body.project_id
    agent      = body.agent
    extra_ctx  = body.context

    if not goal:
        raise HTTPException(status_code=400, detail="goal required")

    api_key = env_get("ANTHROPIC_API_KEY", default="")
    if not api_key:
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY not set")

    plan_id = f"PLAN-{datetime.now().strftime('%Y%m%d%H%M%S')}-{os.urandom(3).hex()}"

    system_prompt = (
        "You are a technical project planner. Decompose the given goal into a list of "
        "atomic engineering tasks. Each task must be independently executable by a single agent. "
        "Return ONLY valid JSON with this exact structure:\n"
        '{"tasks": [{"title": "...", "description": "...", "agent": "...", '
        '"priority": 1|2|3, "depends_on": ["task_title_1"]}]}\n'
        "Use depends_on to reference other tasks by their title (exact match). "
        "Priority: 1=critical, 2=important, 3=nice-to-have. Max 8 tasks."
    )
    user_prompt = f"Goal: {goal}"
    if extra_ctx: user_prompt += f"\n\nContext: {extra_ctx}"
    if project_id: user_prompt += f"\n\nProject: {project_id}"

    try:
        async with create_resilient_client(
            service_name="plans",
            timeout=60.0,
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
        ) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                json={
                    "model":      env_get("AGENT_MODEL", default="claude-3-5-sonnet-20240620"),
                    "max_tokens": 2048,
                    "system":     system_prompt,
                    "messages":   [{"role": "user", "content": user_prompt}],
                },
                timeout=60.0
            )
            resp.raise_for_status()
            raw = resp.json()
        
        text  = raw["content"][0]["text"]
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise HTTPException(status_code=502, detail="LLM returned non-JSON")
        plan_data = json.loads(match.group())
    except Exception as e:
        log.error(f"llm_plan_failed error={e}")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {e}")

    task_specs = plan_data.get("tasks", [])
    if not task_specs:
        raise HTTPException(status_code=502, detail="LLM returned empty task list")

    # Cycle Detection
    try:
        import networkx as nx
        G = nx.DiGraph()
        for spec in task_specs:
            if spec.get("title"): G.add_node(spec["title"])
        for spec in task_specs:
            target = spec.get("title")
            for dep in spec.get("depends_on", []):
                if dep in G: G.add_edge(dep, target)
        if not nx.is_directed_acyclic_graph(G):
            raise HTTPException(status_code=400, detail="Circular dependency in plan")
    except ImportError: pass

    created:      list[dict]  = []
    title_to_id:  dict[str, str] = {}

    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO plans (id, tenant_id, project_id, goal, task_count) VALUES (%s, %s, %s, %s, %s)",
                    (plan_id, tenant_id, project_id, goal, len(task_specs)),
                )
                for spec in task_specs:
                    t_id     = f"TASK-{plan_id}-{os.urandom(3).hex()}"
                    t_agent  = spec.get("agent") or agent or None
                    t_status = "blocked-deps" if spec.get("depends_on") else "pending"
                    title_to_id[spec["title"]] = t_id
                    await cur.execute(
                        """
                        INSERT INTO tasks
                            (id, title, description, status, priority, assigned_agent,
                             project_id, tenant_id, plan_id, created_at, updated_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW(), NOW())
                        """,
                        (t_id, spec["title"], spec.get("description", ""),
                         t_status, int(spec.get("priority", 2)), t_agent,
                         project_id, tenant_id, plan_id),
                    )
                    created.append({
                        "id":       t_id,
                        "title":    spec["title"],
                        "agent":    t_agent,
                        "priority": spec.get("priority", 2),
                        "status":   t_status,
                    })

                for spec in task_specs:
                    t_id = title_to_id[spec["title"]]
                    for dep_title in spec.get("depends_on", []):
                        dep_id = title_to_id.get(dep_title)
                        if dep_id:
                            await cur.execute(
                                "INSERT INTO dependencies (task_id, dependency_id) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                                (t_id, dep_id),
                            )
                await conn.commit()
    except Exception as e:
        log.exception("create_plan_db_error")
        raise HTTPException(status_code=500, detail=str(e))

    await broadcast("plan_created", {
        "plan_id": plan_id, "goal": goal, "task_count": len(created),
    }, tenant_id=tenant_id)

    # Best-effort Neo4j sync
    asyncio.create_task(_sync_plan_to_neo4j_async(plan_id, goal, tenant_id, created, title_to_id, task_specs))

    return {"ok": True, "plan_id": plan_id, "goal": goal, "tasks": created}

class MCTSPlanRequest(BaseModel):
    goal: str
    initial_plan: Optional[Dict[str, Any]] = None
    iterations: int = 100

@router.post("/plan/mcts")
async def plan_mcts(
    req: MCTSPlanRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """Generate optimized plan using MCTS."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT agent_name, reputation_fit_score FROM agent_reputation")
                reputation = await cur.fetchall()
        
        mcts_planner.reputation_data = reputation
        plan = await asyncio.to_thread(mcts_planner.search, req.goal, req.initial_plan or {"tasks": [], "depth": 0}, iterations=req.iterations)
        
        await broadcast("mcts_plan_generated", {
            "goal": req.goal,
            "task_count": len(plan.get("tasks", []))
        }, tenant_id=tenant_id)

        return {"ok": True, "goal": req.goal, "plan": plan}
    except Exception as e:
        log.error(f"MCTS Plan error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /plans ────────────────────────────────────────────────────────────────

@router.get("/plans")
async def list_plans(
    project_id: Optional[str] = None,
    status_filter: Optional[str] = Query(None, alias="status"),
    tenant_id: str = Depends(get_tenant_id)
):
    """List plans for this tenant."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                query  = (
                    "SELECT id, goal, status, task_count, project_id, created_at, updated_at "
                    "FROM plans WHERE tenant_id = %s"
                )
                params: list = [tenant_id]
                if project_id:
                    query += " AND project_id = %s"
                    params.append(project_id)
                if status_filter:
                    query += " AND status = %s"
                    params.append(status_filter)
                query += " ORDER BY created_at DESC LIMIT 50"
                await cur.execute(query, params)
                rows = await cur.fetchall()
        return {"plans": rows, "count": len(rows)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── GET /plans/<plan_id>/graph ────────────────────────────────────────────────

@router.get("/plans/{plan_id}/graph")
async def plan_graph(plan_id: str, tenant_id: str = Depends(get_tenant_id)):
    """
    Task DAG for a plan as nodes + directed edges.
    """
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT id, goal FROM plans WHERE id = %s AND tenant_id = %s",
                    (plan_id, tenant_id),
                )
                plan_row = await cur.fetchone()
                if not plan_row:
                    raise HTTPException(status_code=404, detail="plan not found")

                await cur.execute(
                    """
                    SELECT id, title, status, assigned_agent, priority
                    FROM tasks
                    WHERE plan_id = %s AND tenant_id = %s
                    ORDER BY created_at
                    """,
                    (plan_id, tenant_id),
                )
                task_rows = await cur.fetchall()
                task_ids  = [r["id"] for r in task_rows]

                if not task_ids:
                    return {
                        "plan_id": plan_id,
                        "goal":    plan_row["goal"],
                        "nodes":   [],
                        "edges":   [],
                    }

                await cur.execute(
                    """
                    SELECT d.dependency_id AS source, d.task_id AS target
                    FROM dependencies d
                    WHERE d.task_id = ANY(%s) AND d.dependency_id = ANY(%s)
                    """,
                    (task_ids, task_ids),
                )
                edge_rows = await cur.fetchall()

        return {
            "plan_id": plan_id,
            "goal":    plan_row["goal"],
            "nodes":   task_rows,
            "edges":   edge_rows,
        }
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _sync_plan_to_neo4j_async(plan_id, goal, tenant_id, tasks, title_to_id, task_specs):
    """Async wrapper for Neo4j sync."""
    try:
        from services.streaming.core.circuit import get_breaker
        breaker = get_breaker("neo4j")
        if breaker.state == "open": return

        # This should ideally use an async driver, but for now wrap the sync logic
        def _sync():
            from neo4j import GraphDatabase
            uri  = env_get("NEO4J_URI", default="bolt://localhost:7687")
            user = env_get("NEO4J_USER", default="neo4j")
            pwd  = env_get("NEO4J_PASSWORD", default="neo4j")
            with GraphDatabase.driver(uri, auth=(user, pwd)) as driver:
                with driver.session() as session:
                    session.run(
                        "MERGE (p:Plan {id:$id}) SET p.goal=$goal, p.tenant_id=$tid",
                        id=plan_id, goal=goal, tid=tenant_id,
                    )
                    for t in tasks:
                        session.run(
                            "MERGE (t:Task {id:$id}) "
                            "SET t.title=$title, t.tenant_id=$tid, t.plan_id=$pid "
                            "WITH t MATCH (p:Plan {id:$pid}) MERGE (p)-[:HAS_TASK]->(t)",
                            id=t["id"], title=t["title"], tid=tenant_id, pid=plan_id,
                        )
                    for spec in task_specs:
                        t_id = title_to_id[spec["title"]]
                        for dep_title in spec.get("depends_on", []):
                            dep_id = title_to_id.get(dep_title)
                            if dep_id:
                                session.run(
                                    "MATCH (a:Task {id:$src}),(b:Task {id:$tgt}) "
                                    "MERGE (a)-[:DEPENDS_ON]->(b)",
                                    src=t_id, tgt=dep_id,
                                )
        import asyncio
        await asyncio.to_thread(_sync)
    except Exception as exc:
        log.debug("neo4j_plan_sync_skipped error=%s", exc)
