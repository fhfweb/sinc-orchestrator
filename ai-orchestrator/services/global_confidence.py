"""
global_confidence.py
====================
Unified decision arbiter for SINC AI Engineering System (AES).
Consolidates signals from Graph, Semantic, Predictive, and Reputation layers.
"""
import asyncio
import time
import logging
from dataclasses import dataclass, field
from typing import Optional, List, Dict, Any

log = logging.getLogger("orchestrator.confidence")

@dataclass
class ConfidenceSignal:
    source: str          # "neo4j_graph" | "qdrant_semantic" | "pg_prediction" | "redis_reputation"
    value: float         # 0.0 -> 1.0
    weight: float        # Signal weight
    evidence: str        # Reasoning
    freshness: float = 1.0 # 1.0 = newest, 0.0 = stale

@dataclass
class GlobalConfidenceState:
    task_id: str
    tenant_id: str
    signals: List[ConfidenceSignal] = field(default_factory=list)
    composite_score: float = 0.0
    confidence_level: str = "unknown" # "high" | "medium" | "low" | "critical_low"
    dominant_signal: Optional[str] = None
    conflicting_signals: List[str] = field(default_factory=list)
    recommended_agent: Optional[str] = None
    recommended_strategy: Optional[str] = None
    require_simulation: bool = False
    require_human_gate: bool = False
    reasoning: str = ""
    computed_at_ms: int = 0

async def build_global_confidence(
    task: Any,
    tenant_id: str,
    strategy: Any
) -> GlobalConfidenceState:
    """Consolidates all intelligence layers into a single vector."""
    state = GlobalConfidenceState(task_id=getattr(task, 'id', 'unknown'), tenant_id=tenant_id)
    start_time = time.perf_counter()

    # 1. Parallel Signal Gathering
    from services.intelligence_router import IntelligenceDepth
    
    tasks = []
    # Using the specific signals from prompt
    tasks.append(_collect_graph_signal(task, tenant_id))
    tasks.append(_collect_semantic_signal(task, tenant_id))
    tasks.append(_collect_prediction_signal(task, tenant_id))
    tasks.append(_collect_reputation_signal(task, tenant_id))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for res in results:
        if isinstance(res, Exception):
            log.warning(f"Confidence signal failed: {res}")
            state.signals.append(ConfidenceSignal(source="failed", value=0.5, weight=0.0, evidence=str(res), freshness=0.0))
            continue
        state.signals.append(res)

    # 2. Weighted Score Calculation
    valid_signals = [s for s in state.signals if s.weight > 0]
    if valid_signals:
        total_weight = sum(s.weight * s.freshness for s in valid_signals)
        if total_weight > 0:
            state.composite_score = sum(
                s.value * s.weight * s.freshness for s in valid_signals
            ) / total_weight
        else:
            state.composite_score = 0.5
    else:
        state.composite_score = 0.5

    # 3. Classification
    if state.composite_score >= 0.8:
        state.confidence_level = "high"
    elif state.composite_score >= 0.6:
        state.confidence_level = "medium"
    elif state.composite_score >= 0.4:
        state.confidence_level = "low"
    else:
        state.confidence_level = "critical_low"

    # 4. Conflict Detection
    values = [s.value for s in valid_signals]
    if values and (max(values) - min(values)) > 0.35:
        state.conflicting_signals = [
            s.source for s in valid_signals 
            if abs(s.value - state.composite_score) > 0.25
        ]

    # 5. Recommendation Consolidation
    await _consolidate_recommendations(state, task, tenant_id)

    state.require_simulation = state.confidence_level in ["low", "critical_low"] or len(state.conflicting_signals) >= 2
    state.require_human_gate = state.confidence_level == "critical_low"
    state.computed_at_ms = int((time.perf_counter() - start_time) * 1000)
    
    # dominant signal
    if valid_signals:
        dominant = max(valid_signals, key=lambda s: s.weight * s.freshness)
        state.dominant_signal = dominant.source

    state.reasoning = f"Composite score {state.composite_score:.2f} ({state.confidence_level}). Dominant: {state.dominant_signal}"

    return state

async def _collect_graph_signal(task: Any, tenant_id: str) -> ConfidenceSignal:
    from services.property_graph_manager import get_pg_manager
    pg = get_pg_manager()
    # Direct Neo4j query as per prompt Módulo 1.1
    res = await pg.run_query("""
        MATCH (similar:Task)-[:SIMILAR_TO]->(t:Task {id: $task_id})
        WHERE similar.status = 'done' AND similar.tenant_id = $tenant_id
        WITH COUNT(similar) as similar_count,
             AVG(CASE WHEN similar.status = 'done' THEN 1.0 ELSE 0.0 END) as local_success_rate
        RETURN similar_count, local_success_rate
    """, parameters={"task_id": getattr(task, 'id', 'none'), "tenant_id": tenant_id})
    
    if not res or res[0]["similar_count"] < 3:
        return ConfidenceSignal("neo4j_graph", 0.5, 0.2, "Insufficient history in graph", 0.5)
        
    row = res[0]
    return ConfidenceSignal(
        "neo4j_graph", 
        row["local_success_rate"], 
        0.30, 
        f"{row['similar_count']} similar tasks, {row['local_success_rate']:.0%} success", 
        0.9
    )

async def _collect_semantic_signal(task: Any, tenant_id: str) -> ConfidenceSignal:
    # Simplified semantic signal for arbitration
    return ConfidenceSignal("qdrant_semantic", 0.6, 0.25, "Baseline semantic match", 0.8)

async def _collect_prediction_signal(task: Any, tenant_id: str) -> ConfidenceSignal:
    from services.context_retriever import get_success_prediction
    pred = await get_success_prediction(getattr(task, 'category', 'generic'), getattr(task, 'assigned_agent', 'none'), tenant_id)
    score = pred.get("success_rate", 0.5)
    return ConfidenceSignal("pg_prediction", score, 0.25, f"Historical success rate: {score}", 0.9)

async def _collect_reputation_signal(task: Any, tenant_id: str) -> ConfidenceSignal:
    from services.streaming.core.redis_ import async_get_agent_reputation_score
    score = await async_get_agent_reputation_score(getattr(task, 'assigned_agent', 'none'), tenant_id, default=0.5)
    return ConfidenceSignal("redis_reputation", score, 0.2, f"Agent reputation score: {score}", 1.0)

async def _consolidate_recommendations(state: GlobalConfidenceState, task: Any, tenant_id: str):
    # Weighted voting for agents as per prompt Módulo 1.1
    agent_recommendations = []
    
    # Mocking individual signal recommendations
    graph_agent = "architect" 
    semantic_agent = "coder"
    reputation_agent = getattr(task, 'assigned_agent', "orchestrator")

    agent_recommendations.append((graph_agent, 0.40))
    agent_recommendations.append((semantic_agent, 0.35))
    agent_recommendations.append((reputation_agent, 0.25))

    agent_votes = {}
    for agent, weight in agent_recommendations:
        agent_votes[agent] = agent_votes.get(agent, 0) + weight
    
    state.recommended_agent = max(agent_votes, key=agent_votes.get)

    # Strategy based on confidence level
    if state.confidence_level == "high":
        state.recommended_strategy = "execute_direct"
    elif state.confidence_level == "medium":
        state.recommended_strategy = "execute_with_checkpoints"
    elif state.confidence_level == "low":
        state.recommended_strategy = "decompose_then_execute"
    else:
        state.recommended_strategy = "simulate_then_decide"
