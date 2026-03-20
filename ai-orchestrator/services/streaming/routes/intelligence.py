"""
streaming/routes/intelligence.py
================================
Intelligence-driven endpoints for agent recommendation and task decomposition.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Any, List, Optional
from pydantic import BaseModel

from ..core.auth import get_tenant_id
from ..core.db import async_db
from ..core.redis_ import get_async_redis

router = APIRouter(prefix="/intelligence", tags=["Intelligence"])

class AgentRecommendation(BaseModel):
    agent_name: str
    combined_score: float
    reason: str

class DecomposeRequest(BaseModel):
    goal: str
    project_id: str
    context: Optional[str] = ""


class GoalMissionRequest(BaseModel):
    goal: str
    project_id: str = ""
    context: Optional[str] = ""
    acceptance_criteria: List[str] = []
    constraints: List[str] = []

class SubTask(BaseModel):
    id: str
    title: str
    description: str
    dependencies: List[str] = []

class DecomposeResponse(BaseModel):
    plan_id: str
    tasks: List[SubTask]

@router.get("/agents/recommend", response_model=List[AgentRecommendation])
async def recommend_agents(
    query: str = Query(..., description="Semantic query to find a matching agent"),
    tenant_id: str = Depends(get_tenant_id),
    project_id: str = Query("sinc", description="Project ID for context"),
    category: str = Query("generic", description="Task category")
):
    """
    Recommend the best agents for a given query (Module 7.2).
    Refined Potency weights: 0.70 Semantic Reputation / 0.30 Historical Success.
    """
    import asyncio
    from services.context_retriever import compute_semantic_agent_score, get_success_prediction
    
    # 1. Get candidates
    agent_names = ["orchestrator", "coder", "debugger", "architect"]
    
    # 2. Parallel signal gathering (Modules 2.4 & 5.1)
    semantic_tasks = [compute_semantic_agent_score(query, agent, project_id, tenant_id) for agent in agent_names]
    prediction_tasks = [get_success_prediction(category, agent, tenant_id) for agent in agent_names]
    
    semantic_results = await asyncio.gather(*semantic_tasks)
    prediction_results = await asyncio.gather(*prediction_tasks)

    # 3. Blending and Ranking
    recommendations = []
    
    for i, agent in enumerate(agent_names):
        s_score = semantic_results[i]   # Semantic Rep (0.70)
        p_row   = prediction_results[i] # Historical Success (0.30)
        h_score = p_row.get("success_rate", 0.5)
        
        # Combined Score = 0.7 * Semantic + 0.3 * Historical
        combined = (s_score * 0.70) + (h_score * 0.30)
        
        reason = f"High semantic similarity ({int(s_score*100)}%) to past successful patterns."
        if p_row.get("sample_count", 0) > 5:
            reliable = "consistent" if h_score > 0.8 else "improving"
            reason += f" Agent shows {reliable} performance ({int(h_score*100)}% SR) on this task type."

        recommendations.append(AgentRecommendation(
            agent_name=agent,
            combined_score=round(combined, 4),
            reason=reason
        ))

    return sorted(recommendations, key=lambda x: x.combined_score, reverse=True)[:3]

@router.post("/tasks/decompose", response_model=DecomposeResponse)
async def decompose_task(
    req: DecomposeRequest,
    tenant_id: str = Depends(get_tenant_id)
):
    """
    Decompose a goal using few-shot historical context for Maximum Potency (Module 7.1).
    """
    from services.cognitive_orchestrator import get_orchestrator
    from services.context_retriever import find_similar_past_solutions
    
    orch = get_orchestrator()
    
    try:
        # 1. Fetch few-shot historical patterns from Neo4j
        past_solutions = await find_similar_past_solutions(req.goal, req.project_id, tenant_id)
        
        few_shot_ctx = ""
        if past_solutions:
            few_shot_ctx = "\n=== SUCCESSFUL HISTORICAL DECOMPOSITIONS ===\n"
            for sol in past_solutions[:2]:
                few_shot_ctx += f"Goal: {sol['description']}\nSteps:\n{sol['solution'][:400]}\n---\n"

        # 2. Ask the cognitive pipeline to plan with the extra historical context
        combined_context = (few_shot_ctx + "\n" + (req.context or "")).strip()
        
        plan = orch._planner.plan("decomposition", {
            "description": req.goal, 
            "project_id": req.project_id,
            "context": combined_context
        })
        
        if not plan or not plan.get("steps"):
            raise HTTPException(status_code=500, detail="Decomposition failed to generate steps")

        # 3. Create subtasks
        subtasks = []
        for i, step in enumerate(plan["steps"]):
            subtasks.append(SubTask(
                id=f"sub-{i}",
                title=f"Step {i+1}: {step[:50]}...",
                description=step,
                dependencies=[f"sub-{i-1}"] if i > 0 else []
            ))

        return DecomposeResponse(
            plan_id=f"plan-{hash(req.goal) % 10000}",
            tasks=subtasks
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/goals")
async def goals_orchestration(
    req: GoalMissionRequest,
    tenant_id: str = Depends(get_tenant_id)
) -> dict[str, Any]:
    """
    Canonical goal orchestration endpoint.

    Goals are decomposed into minimally dependent subtasks and immediately
    handed to the scheduler so dependency-free slices can execute in parallel.
    """
    from services.goals_orchestrator import plan_and_execute_goal

    result = await plan_and_execute_goal(
        description=req.goal,
        tenant_id=tenant_id,
        project_id=req.project_id,
        acceptance_criteria=list(req.acceptance_criteria or []),
        constraints=list(req.constraints or []),
        context=req.context or "",
    )

    return result
