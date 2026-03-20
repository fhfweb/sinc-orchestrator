from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from services.agent_switch_policy import pick_candidate_agent, should_switch_agent_via_leaderboard
from services.intelligence_router import IntelligenceStrategy

log = logging.getLogger("orch.autonomous.decisions")


@dataclass
class AutonomousAction:
    action_type: str
    applied: bool
    reasoning: str
    impact: str

async def apply_autonomous_decisions(
    state: dict[str, Any],
    context: dict[str, Any],
    strategy: IntelligenceStrategy,
) -> tuple[dict[str, Any], list[AutonomousAction]]:
    """
    Applies tactical decisions on top of the cognitive signals already gathered.

    This module is not the canonical budget executor used in the LangGraph path,
    but it should remain behaviorally aligned with it so future callers do not
    reintroduce stale decision logic.
    """
    actions: list[AutonomousAction] = []
    updates: dict[str, Any] = {}

    tenant_id = str(state.get("tenant_id") or "")
    prediction = context.get("success_prediction", {}) or {}
    success_rate = float(prediction.get("success_rate", 1.0) or 1.0)

    if strategy.use_prediction and success_rate < 0.40 and state.get("task_type") != "goal":
        updates["task_type"] = "goal"
        actions.append(
            AutonomousAction(
                action_type="auto_decomposed",
                applied=True,
                reasoning=f"success_rate={success_rate:.0%} < 40%",
                impact="Roteado para decomposicao automatica para aumentar a probabilidade de sucesso",
            )
        )

    impact = context.get("impact_report", {}) or {}
    if impact.get("risk_level") == "high":
        current_hint = str(state.get("hint") or "")
        updates["hint"] = (
            current_hint
            + "\nSTRICT MODE ENFORCED: High impact detected. Validate changes before commit."
        ).strip()
        actions.append(
            AutonomousAction(
                action_type="strict_mode_enforced",
                applied=True,
                reasoning=f"risk_level=high (blast_radius={impact.get('blast_radius', 'unknown')})",
                impact="Modo estrito ativado para reduzir risco de mudanca com alto blast radius",
            )
        )

    current_agent = str(
        (state.get("task") or {}).get("assigned_agent")
        or state.get("assigned_agent")
        or ""
    ).strip()
    candidate_agent = pick_candidate_agent(context)
    task_type = str(
        state.get("task_type") or (state.get("task") or {}).get("task_type") or "generic"
    ).strip() or "generic"
    if strategy.use_prediction and candidate_agent and candidate_agent != current_agent:
        should_switch, reason = await should_switch_agent_via_leaderboard(
            tenant_id=tenant_id,
            task_type=task_type,
            current_agent=current_agent,
            candidate_agent=candidate_agent,
        )
        if should_switch:
            updates["assigned_agent"] = candidate_agent
            actions.append(
                AutonomousAction(
                    action_type="switch_agent",
                    applied=True,
                    reasoning=f"{current_agent or 'unassigned'} -> {candidate_agent} ({reason})",
                    impact="Roteamento automatico para o agente com melhor reputacao operacional",
                )
            )

    proactive = context.get("proactive_context", {}) or {}
    pitfalls = proactive.get("pitfalls")
    if pitfalls:
        current_hint = str(updates.get("hint", state.get("hint", "")) or "")
        updates["hint"] = (current_hint + f"\nKNOWN PITFALLS: {pitfalls}").strip()
        actions.append(
            AutonomousAction(
                action_type="known_pitfalls_injected",
                applied=True,
                reasoning="Padroes de erro recorrentes detectados para esta categoria",
                impact="Agente recebe pitfalls conhecidos antes da execucao",
            )
        )

    return updates, actions
