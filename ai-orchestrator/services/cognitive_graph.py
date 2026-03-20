"""
cognitive_graph.py
==================
LangGraph implementation of the Cognitive Pipeline.
Professionalized with Pydantic state and configurable heuristics.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Annotated, Dict, List, Optional, Any

from pydantic import BaseModel, Field, ConfigDict

from langchain_core.messages import BaseMessage
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from services.http_client import create_resilient_client
from services.otel_setup import span
from services.property_graph_manager import get_pg_manager

log = logging.getLogger("orch.cognitive.graph")
DEFAULT_MAX_STEPS = 8
_CONFIDENCE_THRESHOLD = 0.7
_MAX_REFINEMENT_LOOPS = 2


def _state_get(state: CognitiveState | dict[str, Any], key: str, default: Any = None) -> Any:
    if isinstance(state, dict):
        return state.get(key, default)
    return getattr(state, key, default)


def get_orchestrator():
    from services.cognitive_orchestrator import get_orchestrator as _get_orchestrator

    return _get_orchestrator()


def _estimate_confidence(solution: str, description: str) -> float:
    text = str(solution or "").strip()
    prompt = str(description or "").strip().lower()
    if not text:
        return 0.0
    lowered = text.lower()
    if lowered.startswith("[error"):
        return 0.0

    score = 0.20
    if len(text) >= 40:
        score += 0.15
    if len(text) >= 120:
        score += 0.15
    if "```" in text or re.search(r"\b(def|class|return|SELECT|UPDATE|INSERT|async)\b", text):
        score += 0.20
    overlap = 0
    prompt_terms = {token for token in re.findall(r"[a-z0-9_]+", prompt) if len(token) > 2}
    if prompt_terms:
        solution_terms = set(re.findall(r"[a-z0-9_]+", lowered))
        overlap = len(prompt_terms & solution_terms)
        score += min(0.30, overlap * 0.06)
    if any(token in lowered for token in ["not sure", "maybe", "i think", "possibly", "unclear"]):
        score -= 0.20
    return max(0.0, min(1.0, round(score, 3)))

# ── Configuration & State ───────────────────────────────────────────────────

class CognitiveConfig(BaseModel):
    """Reasoning heuristics and safety limits."""
    max_steps: int = DEFAULT_MAX_STEPS
    quality_threshold: float = 0.7
    degradation_threshold: float = 0.3
    max_refinement_loops: int = 2
    llm_max_tokens: int = 1024

class CognitiveState(BaseModel):
    """The state passed between nodes in the cognitive graph."""
    task: Dict[str, Any]
    description: str
    task_type: str
    project_id: str
    tenant_id: str
    execution_path: str = "standard" # instant, fast, standard, deep
    
    # Internal routing & results
    solution: Optional[str] = None
    steps: List[str] = []
    planner_name: str = "orchestrator"
    cache_level: str = "none"
    llm_needed: bool = True
    llm_used: bool = False
    hint: str = ""
    proactive_context: Optional[dict] = None
    confidence: float = 0.5
    verified_by_vnode: bool = False
    status: str = "active" # active, awaiting_user, completed
    user_feedback: Optional[str] = None
    interrupt_requested: bool = False
    
    # Metrics
    tokens_saved: int = 0
    tokens_used: int = 0
    start_time: float = Field(default_factory=time.perf_counter)
    latency_ms: float = 0.0
    error: Optional[str] = None
    confidence_score: float = 1.0   # heuristic from quality_gate
    _refinement_loop: int = 0      # loop counter (L3/L4 retry)
    
    # Adaptive Autonomy
    intelligence_strategy: Optional[dict] = None
    autonomous_actions: List[dict] = []
    global_confidence: Optional[Any] = None
    decision_budget: Optional[Any] = None

    # Custom field to pass pooled resources
    http_client: Any = None 

    model_config = ConfigDict(arbitrary_types_allowed=True)

# ── Graph Nodes ──────────────────────────────────────────────────────────────

def l0_rules_node(state: CognitiveState) -> Dict:
    """Evaluate dynamic rules."""
    orch = get_orchestrator()
    rules = getattr(orch, "_rules", None) # Safe access
    
    if rules:
        try:
            task_type = _state_get(state, "task_type", "")
            task_payload = _state_get(state, "task", {}) or {}
            rule = rules.evaluate(task_type, task_payload.get("error_signature"))
            if rule and rule.action.startswith("prefer_agent:"):
                preferred = rule.action.split(":")[1]
                return {"hint": f"prefer_agent:{preferred}"}
        except Exception as exc:
            log.debug("rules_evaluate_error error=%s", exc)
            
    return {}

async def goal_decomposer_node(state: CognitiveState) -> Dict:
    """Module 7.1: Goal Orchestrator. Decomposes high-level goals into subtasks."""
    if state.task_type != "goal":
        return {}
        
    from services.streaming.routes.intelligence import decompose_task, DecomposeRequest
    try:
        from services.context_retriever import find_similar_past_solutions
        history = await find_similar_past_solutions(state.description, state.project_id, state.tenant_id)
        
        history_hint = ""
        if history:
            best = history[0]
            solution_preview = (best.get('solution') or "")[:300]
            history_hint = f"\n[HISTORICAL-GUIDE] Sibling goal was split into: {solution_preview}"

        res = await decompose_task(
            DecomposeRequest(goal=state.description, project_id=state.project_id),
            tenant_id=state.tenant_id
        )
        steps = [t.description for t in res.tasks]
        return {
            "steps": steps, 
            "hint": f"Goal decomposed into {len(steps)} steps.{history_hint}",
            "planner_name": "goal_orchestrator"
        }
    except Exception as exc:
        log.warning("goal_decomposition_failed error=%s", exc)
        return {"error": f"decomposition_failed: {exc}"}

async def memory_lookup_node(state: CognitiveState) -> Dict:
    """Module 1.1 & 2.1: Global Confidence & Decision Budget."""
    from services.intelligence_router import classify_task_intelligence
    from services.global_confidence import build_global_confidence
    from services.decision_budget import execute_decisions_with_budget
    from services.cognitive_orchestrator import get_orchestrator
    orch = get_orchestrator()
    
    updates = {}
    path = state.execution_path
    
    # 1. Tiered Arbitration
    if path in ["instant", "fast"]:
        confidence = {
            "composite_score": 0.85, "confidence_level": "high",
            "recommended_strategy": "execute_direct", "require_simulation": False,
            "require_human_gate": False, "reasoning": "Fast path triggered."
        }
    else:
        strategy = await classify_task_intelligence(
            state.description, state.task_type, state.tenant_id,
            primary_file=state.task.get("primary_file")
        )
        updates["intelligence_strategy"] = strategy.__dict__ if hasattr(strategy, "__dict__") else strategy
        confidence = await build_global_confidence(state.task, state.tenant_id, strategy)
    
    updates["global_confidence"] = confidence
    reas = getattr(confidence, 'reasoning', 'Fast Path') if not isinstance(confidence, dict) else confidence.get('reasoning', 'Fast Path')
    updates["hint"] = state.hint + f"\n[CONFIDENCE] {reas}"

    # 3. Apply Autonomous Decisions with Budget
    modified_task_proxy = state.task.copy()
    if "assigned_agent" not in modified_task_proxy:
        modified_task_proxy["assigned_agent"] = state.planner_name
    
    from types import SimpleNamespace
    task_obj = SimpleNamespace(**modified_task_proxy)
    
    new_task_obj, budget = await execute_decisions_with_budget(task_obj, confidence, state.tenant_id)
    
    updates["decision_budget"] = budget
    updates["autonomous_actions"] = [{"type": a} for a in getattr(budget, 'applied', [])]
    
    updates["planner_name"] = getattr(new_task_obj, "assigned_agent", state.planner_name)
    if getattr(new_task_obj, "status", "") == "awaiting-gate":
        updates["error"] = "human_gate_required"
        updates["solution"] = "[paused: human gate required]"

    # 4. Check Memory L1/L2
    memory = orch.registry.get("memory")
    if memory and (not updates.get("intelligence_strategy") or updates["intelligence_strategy"].get("depth") != "instant"):
        try:
            hit = await memory.resolve(state.task_type, state.description, state.project_id, state.tenant_id)
            if hit:
                updates.update({
                    "solution": hit.get("solution", ""),
                    "cache_level": hit.get("cache_level", "L?"),
                    "tokens_saved": hit.get("tokens_saved", 200),
                    "llm_needed": False,
                    "confidence": 0.95
                })
        except Exception as exc:
            log.warning("memory_resolve_error error=%s", exc)
            
    return updates

async def graph_reasoning_node(state: CognitiveState) -> Dict:
    """Canonical graph reasoning path: GraphRAG + GoT."""
    if state.solution: return {}

    from services.cognitive_orchestrator import get_orchestrator
    from services.graph_reasoning_adapter import resolve_graph_reasoning

    orch = get_orchestrator()
    result = await resolve_graph_reasoning(
        state.description, state.task_type, state.project_id, 
        state.tenant_id, orch=orch
    )

    structural_hint = f"\nStructural Analysis: {result.structural_context}" if result.structural_context else ""

    if result.solution:
        return {
            "solution": result.solution,
            "steps": result.steps or [],
            "cache_level": result.cache_level or "neo4j_existing",
            "tokens_saved": result.tokens_saved,
            "llm_needed": False,
            "hint": state.hint + structural_hint,
            "confidence": result.confidence,
        }

    return {"hint": state.hint + structural_hint, "confidence": result.confidence}

async def hybrid_planner_node(state: CognitiveState) -> Dict:
    """MCTS or Deterministic planning."""
    if state.solution: return {}
    
    from services.cognitive_orchestrator import get_orchestrator
    orch = get_orchestrator()
    planner = orch.registry.get("planner")
    
    if planner:
        try:
            plan = await planner.plan(
                state.task_type,
                {"project_id": state.project_id, "description": state.description, "tenant_id": state.tenant_id}
            )
            if not plan.get("llm_needed", True) and plan.get("steps"):
                return {
                    "solution": " -> ".join(plan["steps"]),
                    "steps": plan["steps"],
                    "planner_name": plan.get("planner", "none"),
                    "cache_level": f"planner_{plan.get('planner', 'none')}",
                    "tokens_saved": 2000,
                    "llm_needed": False,
                    "confidence": plan.get("confidence", 0.0)
                }
            return {
                "steps": plan.get("steps", []),
                "planner_name": plan.get("planner", "none"),
                "llm_needed": True,
                "hint": plan.get("hint", "")
            }
        except Exception as e:
            log.warning("planner_failed error=%s", e)
    return {"llm_needed": True}

async def llm_solver_node(state: CognitiveState) -> Dict:
    with span("cognitive.llm_solver", tenant_id=state.tenant_id, project_id=state.project_id):
        """Final LLM Execution Gate. Handles distributed retries for rate limits."""
        if state.solution: return {}
        
        from services.cognitive_orchestrator import get_orchestrator
        orch = get_orchestrator()
        
        solver = orch.registry.get("llm_solver")
        if not solver:
            from services.llm_solver import LLMSolverService
            solver = LLMSolverService(state.http_client or create_resilient_client(service_name="cognitive-graph"))
        
        try:
            res = await solver.solve(state.description, state.task_type, state.steps, state.hint, state.tenant_id)
            return {"solution": res.solution, "tokens_used": res.tokens_used, "cache_level": "llm", "llm_used": True}
        except Exception as exc:
            log.error("llm_node_error id=%s error=%s", state.task.get("id"), exc)
            return {"error": str(exc), "solution": f"[llm_error: {exc}]"}
    
async def learn_and_store_node(state: CognitiveState) -> Dict:
    with span("cognitive.learn_and_store", tenant_id=_state_get(state, "tenant_id", "")):
        """Evolutionary Memory Layer with Autonomous Verification Gate."""
        solution = _state_get(state, "solution")
        error = _state_get(state, "error")
        succeeded = bool(solution and not error and "[error" not in (solution or "").lower())
        payload = state.model_dump() if hasattr(state, "model_dump") else dict(state)
        validation_decision = {
            "verified": False,
            "verification_source": str(_state_get(state, "verification_source", "") or "").strip().lower() or "unverified",
            "validation_passed": bool(_state_get(state, "validation_passed", False)),
            "reason": "missing_validation_gate",
            "checks": [],
        }
        if succeeded:
            try:
                from services.code_validator_agent import get_code_validator_agent

                validation_decision = (
                    await get_code_validator_agent().validate_for_memory(payload)
                ).as_dict()
            except Exception as exc:
                log.warning("code_validator_gate_failed error=%s", exc)

        is_verified = bool(validation_decision.get("verified", False))
    
        try:
            from services.memory_evolution import generate_and_store_lesson

            payload["validation_gate"] = validation_decision
            await generate_and_store_lesson(payload, solution or "", succeeded, error, verified=is_verified)
        except Exception as e:
            log.warning("learning_failed error=%s", e)
    
        try:
            from services.cognitive_orchestrator import get_orchestrator
    
            orch = get_orchestrator()
            got = getattr(orch, "_got", None)
            memory = getattr(orch, "_memory", None)
            l2 = getattr(memory, "l2", None) if memory else None
            embedder = getattr(l2, "embedder_func", None) if l2 else None
            if got and solution:
                got.persist_reasoning(
                    state.description,
                    state.task_type,
                    solution,
                    state.steps or [],
                    succeeded,
                    embedder,
                )
        except Exception as e:
            log.debug("got_persist_failed error=%s", e)
    
        try:
            import context_retriever
    
            retriever = context_retriever.ContextRetriever()
            await asyncio.to_thread(
                retriever.store_solution,
                _state_get(state, "description", ""),
                solution or "",
                _state_get(state, "project_id", ""),
                _state_get(state, "tenant_id", "local"),
                _state_get(state, "task_type", ""),
                [],
            )
        except Exception as e:
            log.debug("context_solution_store_failed error=%s", e)
    
        try:
            from neo4j import GraphDatabase
    
            relation = "SUCCEEDED_ON" if succeeded else "FAILED_ON"
            query = f"""
                MERGE (t:Task {{task_id: $task_id}})
                MERGE (o:Outcome {{cache_level: $cache_level}})
                MERGE (t)-[:{relation}]->(o)
            """
    
            def _write_relation():
                driver = GraphDatabase.driver(
                    os.getenv("NEO4J_URI", "bolt://localhost:7687"),
                    auth=(
                        os.getenv("NEO4J_USER", "neo4j"),
                        os.getenv("NEO4J_PASS", os.getenv("NEO4J_AUTH", "neo4j/neo4j").split("/")[-1]),
                    ),
                )
                try:
                    with driver.session() as session:
                        session.run(
                            query,
                            task_id=(_state_get(state, "task", {}) or {}).get("id"),
                            cache_level=_state_get(state, "cache_level", "none"),
                        )
                finally:
                    driver.close()
    
            await asyncio.to_thread(_write_relation)
        except Exception as e:
            log.debug("neo4j_learning_relation_failed error=%s", e)
    
        try:
            from services.streaming.core.redis_ import get_async_redis
    
            redis = get_async_redis()
            if redis:
                tenant_id = str(_state_get(state, "tenant_id", "local"))
                task_type = str(_state_get(state, "task_type", "generic"))
                agent_name = str(_state_get(state, "planner_name", "orchestrator"))
                key = f"sinc:leaderboard:{tenant_id}:{task_type}"
                current = await redis.zscore(key, agent_name)
                reward = 1.0 if succeeded else 0.0
                next_score = reward if current is None else round((float(current) * 0.8) + (reward * 0.2), 4)
                pipe = redis.pipeline()
                pipe.zadd(key, {agent_name: next_score})
                pipe.expire(key, 60 * 60 * 24 * 30)
                await pipe.execute()
        except Exception as e:
            log.debug("leaderboard_update_failed error=%s", e)
        return {}
    
    
async def code_verification_node(state: CognitiveState) -> Dict:
    with span("cognitive.code_verification", task_id=(_state_get(state, "task", {}) or {}).get("task_id")):
        """Module 9.1: Lightweight syntax validation only. Does not auto-verify long-term memory."""
        solution = _state_get(state, "solution")
        if not solution or "```" not in solution:
            return {"verified_by_vnode": False, "validation_passed": False, "verification_source": "syntax_check"}
        
        # Extract code blocks
        code_blocks = re.findall(r"```(?:python)?\n(.*?)\n```", solution, re.DOTALL)
        if not code_blocks:
            return {"verified_by_vnode": False, "validation_passed": False, "verification_source": "syntax_check"}
        
        import tempfile
        import subprocess
        
        is_valid = True
        for block in code_blocks:
            with tempfile.NamedTemporaryFile(suffix=".py", delete=False) as tmp:
                tmp.write(block.encode("utf-8"))
                tmp_path = tmp.name
            try:
                # Syntax check only for safety in this node
                res = subprocess.run([sys.executable, "-m", "py_compile", tmp_path], 
                                     capture_output=True, timeout=5)
                if res.returncode != 0:
                    is_valid = False
                    break
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        
        return {"verified_by_vnode": False, "validation_passed": is_valid, "verification_source": "syntax_check"}
    
async def quality_gate_node(state: CognitiveState) -> Dict:
    with span("cognitive.quality_gate", confidence=_state_get(state, "confidence", 0.0)):
        """Evaluate solution confidence."""
        solution = _state_get(state, "solution", "")
        confidence = float(_state_get(state, "confidence_score", 0.0) or 0.0)
        loop_count = int(_state_get(state, "_refinement_loop", 0) or 0)
        if solution:
            confidence = max(confidence, _estimate_confidence(solution, _state_get(state, "description", "")))
        if not solution:
            return {"confidence_score": confidence}
        if confidence >= _CONFIDENCE_THRESHOLD:
            return {}
        if loop_count >= _MAX_REFINEMENT_LOOPS:
            return {}
        return {
            "solution": None,
            "confidence_score": confidence,
            "_refinement_loop": loop_count + 1,
        }
        
    
# ── Routing Logic ────────────────────────────────────────────────────────────

def should_refine(state: CognitiveState) -> str:
    config = CognitiveConfig()
    if state.solution and state.confidence_score >= config.quality_threshold:
        return "approved"
    if state._refinement_loop >= config.max_refinement_loops:
        return "force_approve"
    return "refine"

def should_continue(state: CognitiveState) -> str:
    return "end" if state.solution else "continue"

# ── Graph Construction ───────────────────────────────────────────────────────


async def user_intervention_node(state: CognitiveState) -> Dict:
    """Phase 12: Human-in-the-Loop Intervention.
    Pauses execution and waits for user feedback if interrupt is requested or confidence is low.
    """
    with span("cognitive.user_intervention", task_id=state.task.get("id")):
        if not state.interrupt_requested and state.confidence > 0.4:
            return {"status": "active"}
        
        log.info("hil_intervention_triggered task_id=%s", state.task.get("id"))
        # In a real system, this would wait for a Redis pub/sub or a specialized HIL service.
        # For this roadmap, we mark it as awaiting_user and provide a placeholder for feedback.
        return {"status": "awaiting_user", "hint": "User intervention required due to low confidence."}

def build_cognitive_graph():
    workflow = StateGraph(CognitiveState)
    
    # Nodes
    workflow.add_node("l0_rules", l0_rules_node)
    workflow.add_node("goal_decomposer", goal_decomposer_node)
    workflow.add_node("memory_lookup", memory_lookup_node)
    workflow.add_node("graph_reasoning", graph_reasoning_node)
    workflow.add_node("hybrid_planner", hybrid_planner_node)
    workflow.add_node("llm_solver", llm_solver_node)
    workflow.add_node("quality_gate", quality_gate_node)
    workflow.add_node("learn_and_store", learn_and_store_node)
    workflow.add_node("user_intervention", user_intervention_node)
    workflow.add_edge("user_intervention", "learn_and_store")
    workflow.add_node("code_validator", code_verification_node)

    # Topology
    workflow.set_entry_point("l0_rules")
    workflow.add_edge("l0_rules", "goal_decomposer")
    workflow.add_edge("goal_decomposer", "memory_lookup")
    
    workflow.add_conditional_edges("memory_lookup", should_continue, {
        "end": "quality_gate", "continue": "graph_reasoning"
    })
    workflow.add_conditional_edges("graph_reasoning", should_continue, {
        "end": "quality_gate", "continue": "hybrid_planner"
    })
    workflow.add_conditional_edges("hybrid_planner", should_continue, {
        "end": "quality_gate", "continue": "llm_solver"
    })
    
    workflow.add_edge("llm_solver", "code_validator")
    workflow.add_edge("code_validator", "quality_gate")
    workflow.add_conditional_edges("quality_gate", should_refine, {
        "approved": "user_intervention",
        "force_approve": "user_intervention",
        "refine": "graph_reasoning" 
    })
    workflow.add_edge("user_intervention", "learn_and_store")
    workflow.add_edge("learn_and_store", END)

    
    workflow.add_edge("learn_and_store", END)
    
    return workflow.compile(checkpointer=MemorySaver())

_graph = None

def get_cognitive_graph():
    global _graph
    if _graph is None:
        _graph = build_cognitive_graph()
    return _graph
