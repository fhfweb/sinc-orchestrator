"""
simulation_engine.py
====================
Simulation motor for SINC AI Engineering System.
Evaluates agent/strategy candidates against historical success models (PG, Neo4j) before acting.
"""
import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Any, Dict
from uuid import uuid4

log = logging.getLogger("orchestrator.simulation")

@dataclass
class SimulatedStrategy:
    name: str
    agent: str
    approach_description: str
    estimated_success_rate: float
    estimated_duration_minutes: float
    estimated_cost_usd: float
    risks: List[str] = field(default_factory=list)
    evidence_source: str = ""

@dataclass
class SimulationResult:
    task_id: str
    strategies_evaluated: List[SimulatedStrategy]
    recommended: SimulatedStrategy
    reasoning: str
    simulation_cost_ms: int

async def generate_experimental_strategy(
    task: Any,
    historical_strategies: List[SimulatedStrategy],
    tenant_id: str
) -> Optional[SimulatedStrategy]:
    """
    Módulo 2.1 — Geração de estratégia experimental desafiadora.
    """
    if not historical_strategies:
        return None

    # Analisar padrão das estratégias históricas
    agents = [s.agent for s in historical_strategies]
    dominant_agent = max(set(agents), key=agents.count)
    
    # Identificar o challenger (segundo melhor ou agente diferente)
    from services.streaming.core.db import async_db
    async with async_db(tenant_id=tenant_id) as conn:
        challenger = await conn.execute("""
            SELECT agent_name, success_rate FROM task_success_prediction
            WHERE category = $1 AND tenant_id = $2 AND agent_name != $3
            ORDER BY success_rate DESC LIMIT 1
        """, (getattr(task, 'category', 'generic'), tenant_id, dominant_agent))
        challenger_row = await challenger.fetchone()

    challenger_agent = challenger_row["agent_name"] if challenger_row else dominant_agent
    
    # Lógica de exploração: quebrar padrões
    if len(historical_strategies) >= 2 and all("linear" in s.approach_description.lower() for s in historical_strategies):
        experimental_name = "parallel_execution"
        approach = "executar subtasks independentes em paralelo — potencial de 2x velocidade"
    else:
        experimental_name = f"challenger_{challenger_agent}"
        approach = f"usar {challenger_agent} ao invés do habitual {dominant_agent} — descoberta de eficiência"

    base_rate = challenger_row["success_rate"] if challenger_row else 0.4
    
    return SimulatedStrategy(
        name=experimental_name,
        agent=challenger_agent,
        approach_description=approach,
        estimated_success_rate=base_rate * 0.75, # Desconto de incerteza por ser exploratório
        estimated_duration_minutes=max([s.estimated_duration_minutes for s in historical_strategies]) * 1.2,
        estimated_cost_usd=sum([s.estimated_cost_usd for s in historical_strategies]) / len(historical_strategies),
        risks=["abordagem experimental — sem histórico validado"],
        evidence_source="exploração — variância estratégica"
    )

async def simulate_execution_strategies(
    task: Any,
    confidence: Any,
    tenant_id: str,
    n_strategies: int = 3
) -> Optional[SimulationResult]:
    """
    Evaluates N strategies (historical vs templates) without LLM overhead.
    Aligned with prompt Módulo 3.1.
    """
    start_ms = int(time.perf_counter() * 1000)
    
    historical = await _get_historical_strategies(task, tenant_id, limit=n_strategies)
    template = await _get_template_strategies(task, tenant_id, limit=n_strategies)
    
    candidates = historical + template
    
    # Sprint 2: Exploratory Simulation
    experimental = await generate_experimental_strategy(task, historical, tenant_id)
    if experimental:
        candidates.append(experimental)

    if not candidates:
        return None

    evaluated = await asyncio.gather(*[
        _evaluate_strategy(s, task, tenant_id) for s in candidates
    ])

    # Exploratory selection logic: experimental wins only with > 15% uplift
    historical_best = max([s for s in evaluated if "experimental" not in s.name], key=lambda s: s.estimated_success_rate, default=None)
    experimental_cand = next((s for s in evaluated if "experimental" in s.name), None)

    if experimental_cand and historical_best and experimental_cand.estimated_success_rate > historical_best.estimated_success_rate + 0.15:
        best = experimental_cand
        reasoning = f"Experimental strategy '{best.name}' selected due to significant uplift prediction."
    else:
        # Composite Score calculation as per prompt
        scored = sorted(evaluated, key=lambda s: (
            0.50 * s.estimated_success_rate +
            0.30 * (1.0 - min(s.estimated_duration_minutes / 60, 1.0)) +
            0.20 * (1.0 - min(s.estimated_cost_usd / 1.0, 1.0))
        ), reverse=True)
        best = scored[0]
        reasoning = f"Strategy '{best.name}' recommended with {best.estimated_success_rate:.0%} success rate."
    
    return SimulationResult(
        task_id=getattr(task, 'id', 'unknown'),
        strategies_evaluated=evaluated,
        recommended=best,
        reasoning=reasoning,
        simulation_cost_ms=int(time.perf_counter() * 1000) - start_ms
    )

async def _get_historical_strategies(task: Any, tenant_id: str, limit: int) -> List[SimulatedStrategy]:
    from services.property_graph_manager import get_pg_manager
    pg = get_pg_manager()
    # Exact Neo4j query from prompt Módulo 3.1
    results = await pg.run_query("""
        MATCH (similar:Task)-[:SIMILAR_TO]->(t:Task {id: $task_id})
        WHERE similar.status = 'done' AND similar.tenant_id = $tenant_id
        MATCH (a:Agent)-[:SUCCEEDED_ON]->(similar)
        MATCH (similar)-[:RESOLVED_BY]->(s:Solution)
        WITH a, s, similar,
             COUNT(*) as usage_count,
             AVG(similar.duration_minutes) as avg_duration
        RETURN a.name as agent_name,
               s.description as approach,
               usage_count,
               avg_duration,
               similar.category as category
        ORDER BY usage_count DESC LIMIT $limit
    """, parameters={"task_id": getattr(task, 'id', 'none'), "tenant_id": tenant_id, "limit": limit})
    
    return [SimulatedStrategy(
        name=f"historical_{r['agent_name']}",
        agent=r["agent_name"],
        approach_description=r["approach"][:200],
        estimated_success_rate=0.0,
        estimated_duration_minutes=r["avg_duration"] or 30.0,
        estimated_cost_usd=0.0,
        evidence_source=f"used {r['usage_count']}x in similar tasks"
    ) for r in results]

async def _get_template_strategies(task: Any, tenant_id: str, limit: int) -> List[SimulatedStrategy]:
    return [SimulatedStrategy(
        name="standard_template",
        agent="orchestrator",
        approach_description="Standard decomposition approach.",
        estimated_success_rate=0.0,
        estimated_duration_minutes=45.0,
        estimated_cost_usd=0.0,
        evidence_source="System Template"
    )]

async def _evaluate_strategy(strategy: SimulatedStrategy, task: Any, tenant_id: str) -> SimulatedStrategy:
    from services.context_retriever import get_success_prediction
    pred = await get_success_prediction(getattr(task, 'category', 'generic'), strategy.agent, tenant_id)
    
    strategy.estimated_success_rate = pred.get("success_rate", 0.5)
    strategy.estimated_cost_usd = 0.005 # Placeholder
    # Risks check simulated
    strategy.risks = ["potential_logic_error"]
    
    return strategy

async def apply_simulation_learnings(simulation: SimulationResult, succeeded: bool, tenant_id: str):
    # Feedback loop as per prompt Módulo 3.2
    actual = 1.0 if succeeded else 0.0
    error = abs(simulation.recommended.estimated_success_rate - actual)
    
    if error < 0.2:
        log.info(f"SIMULATION_CONFIRMED: Strategy {simulation.recommended.name} for task {simulation.task_id}")
    else:
        log.warning(f"SIMULATION_MISPREDICTION: error={error:.2f} for task {simulation.task_id}")

    # Sprint 4: Persist evaluation for Autonomy Score
    try:
        from services.streaming.core.db import async_db
        async with async_db(tenant_id=tenant_id) as conn:
            # Update simulation_evaluations
            await conn.execute("""
                INSERT INTO simulation_evaluations (task_id, tenant_id, predicted_success, actual_success, strategy_name, error_delta)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (simulation.task_id, tenant_id, simulation.recommended.estimated_success_rate, actual, simulation.recommended.name, error))
            
            # Sprint 2: Update experimental results if relevant
            if "experimental" in simulation.recommended.name or "challenger" in simulation.recommended.name or "parallel" in simulation.recommended.name:
                await conn.execute("""
                    INSERT INTO experimental_strategy_results (task_id, tenant_id, strategy_name, agent_used, predicted_success, actual_success, was_selected)
                    VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                """, (simulation.task_id, tenant_id, simulation.recommended.name, simulation.recommended.agent, simulation.recommended.estimated_success_rate, succeeded))
    except Exception as e:
        log.warning(f"failed_to_persist_simulation_eval: {e}")
