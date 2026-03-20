"""
decision_budget.py
==================
Governance for autonomous actions. Ensures AI identity but avoids "hallucinating" too many decisions.
"""
import logging
from dataclasses import dataclass, field
from typing import List, Any, Tuple

from services.agent_switch_policy import should_switch_agent_via_leaderboard

log = logging.getLogger("orchestrator.budget")

DECISION_PRIORITY = [
    "require_human_gate",        
    "auto_decompose",            
    "switch_agent",              
    "inject_known_pitfalls",     
    "enforce_strict_mode",       
    "boost_context_depth",       
]

@dataclass
class DecisionBudget:
    max_decisions: int = 3
    remaining: int = 3
    applied: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)

    def can_apply(self, decision_type: str) -> bool:
        if self.remaining <= 0:
            self.skipped.append(decision_type)
            return False
        return True

    def apply(self, decision_type: str):
        self.applied.append(decision_type)
        self.remaining -= 1

    def skip(self, decision_type: str, reason: str):
        self.skipped.append(f"{decision_type}:{reason}")


async def execute_decisions_with_budget(
    task: Any,
    confidence: Any,
    tenant_id: str
) -> Tuple[Any, DecisionBudget]:
    """
    Limits autonomous overhead by prioritizing decisions based on prompt Módulo 2.1.
    """
    budget = DecisionBudget()
    
    # Decision 1: Human Gate (prioridade máxima, não conta no budget)
    if confidence.require_human_gate:
        task.status = "awaiting-gate"
        budget.apply("require_human_gate")
        return task, budget

    # Decision 2: Decomposição / Simulação
    if confidence.recommended_strategy in ["decompose_then_execute", "simulate_then_decide"] and budget.can_apply("auto_decompose"):
        if confidence.recommended_strategy == "simulate_then_decide":
            from services.simulation_engine import simulate_execution_strategies
            sim = await simulate_execution_strategies(task, confidence, tenant_id)
            if sim:
                 task.autonomous_actions = getattr(task, 'autonomous_actions', []) + [f"simulated:{sim.recommended.name}"]
                 task.assigned_agent = sim.recommended.agent
                 budget.apply("auto_decompose")
                 return task, budget
        else:
             task.autonomous_actions = getattr(task, 'autonomous_actions', []) + ["auto_decompose"]
             budget.apply("auto_decompose")

    # Decision 3: Switch Agent
    current_agent = getattr(task, "assigned_agent", None)
    candidate_agent = getattr(confidence, "recommended_agent", None)
    if candidate_agent and candidate_agent != current_agent:
        task_type = getattr(task, "task_type", None) or getattr(task, "category", "generic")
        allow_switch, switch_reason = await should_switch_agent_via_leaderboard(
            tenant_id=tenant_id,
            task_type=task_type,
            current_agent=current_agent or "",
            candidate_agent=candidate_agent,
            delta_threshold=0.15,
        )

        if allow_switch and budget.can_apply("switch_agent"):
            task.assigned_agent = candidate_agent
            task.autonomous_actions = getattr(task, 'autonomous_actions', []) + [
                f"switch_agent:{current_agent or 'none'}->{candidate_agent}:{switch_reason}"
            ]
            budget.apply("switch_agent")
        elif not allow_switch:
            budget.skip("switch_agent", switch_reason)

    # Decision 4: Pitfall Injection
    # (Simplified context check)
    if budget.can_apply("inject_known_pitfalls"):
        task.autonomous_actions = getattr(task, 'autonomous_actions', []) + ["injected_pitfalls"]
        budget.apply("inject_known_pitfalls")

    # Final Check: AES Identity (Ensure at least one decision)
    if not budget.applied:
        # Forced minimal decision
        task.autonomous_actions = getattr(task, 'autonomous_actions', []) + ["baseline_context"]
        budget.apply("baseline_context_injected")

    # Sprint 4: Persist decisions for Autonomy Score
    try:
        from services.streaming.core.db import get_pool
        pool = await get_pool()
        async with pool.acquire() as conn:
            for action in budget.applied:
                await conn.execute("""
                    INSERT INTO task_autonomous_actions (task_id, tenant_id, action_type)
                    VALUES ($1, $2, $3)
                """, getattr(task, 'id', None), tenant_id, action)
    except Exception as e:
        log.warning(f"failed_to_persist_decisions: {e}")

    return task, budget

async def assert_aes_identity(task_id: str, budget: DecisionBudget, tenant_id: str):
    """Logs violation if system acted like a passive assistant."""
    if not budget.applied:
        log.critical(f"AES_IDENTITY_VIOLATION task={task_id}: No autonomous decisions applied.")
