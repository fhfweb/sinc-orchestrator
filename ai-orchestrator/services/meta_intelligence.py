from dataclasses import dataclass
import json
import logging
from enum import Enum
from typing import Optional, Dict
from services.intelligence_router import IntelligenceStrategy, IntelligenceDepth

log = logging.getLogger("orch.meta.intelligence")

class ErrorCostLevel(Enum):
    LOW = 1
    MEDIUM = 2
    HIGH = 3

@dataclass
class IntelligenceDecision:
    use_graph_reasoning: bool
    use_semantic_retrieval: bool
    use_predictive_model: bool
    use_proactive_injection: bool
    use_llm_synthesis: bool
    use_quality_gate: bool
    timeout_budget_ms: int
    reasoning: str

class MetaIntelligenceOrchestrator:
    """
    Module 4.1: Decide como usar cada tipo de inteligência.
    """
    async def decide(
        self,
        description: str,
        task_type: str,
        tenant_id: str,
        depth_val: str,
        primary_file: Optional[str] = None
    ) -> IntelligenceDecision:
        
        # 1. Estimar custo de erro
        error_cost = await self._estimate_error_cost(description, task_type)
        
        # 2. Verificar histórico de utilidade (Module 4.1)
        layer_utility = await self._get_layer_utility_history(task_type, tenant_id)
        
        # 3. Decisões estruturais
        use_neo4j = (
            depth_val in ["deep", "maximum"] and
            bool(primary_file) and
            layer_utility.get("L2", 0.5) > 0.4 and
            error_cost != ErrorCostLevel.LOW
        )
        
        use_qdrant = (
            depth_val != "instant" and
            layer_utility.get("L1", 0.5) > 0.2
        )
        
        use_quality_gate = (
            error_cost == ErrorCostLevel.HIGH and
            depth_val in ["deep", "maximum"]
        )

        return IntelligenceDecision(
            use_graph_reasoning=use_neo4j,
            use_semantic_retrieval=use_qdrant,
            use_predictive_model=True,
            use_proactive_injection=True,
            use_llm_synthesis=depth_val != "instant",
            use_quality_gate=use_quality_gate,
            timeout_budget_ms=1000 if depth_val == "maximum" else 400,
            reasoning=f"Cost={error_cost.name}, Utility_L2={layer_utility.get('L2', 0):.2f}, Strategy={depth_val}"
        )

    async def _estimate_error_cost(self, description: str, category: str) -> ErrorCostLevel:
        combined = f"{description} {category}".lower()
        if any(w in combined for w in ["prod", "deploy", "migration", "auth", "security", "payment", "delete"]):
            return ErrorCostLevel.HIGH
        if any(w in combined for w in ["docs", "readme", "comment", "typo", "style"]):
            return ErrorCostLevel.LOW
        return ErrorCostLevel.MEDIUM

    async def _get_layer_utility_history(self, category: str, tenant_id: str) -> Dict[str, float]:
        from services.streaming.core.db import get_pool
        from services.streaming.core.redis_ import get_async_redis
        
        r = get_async_redis()
        cache_key = f"sinc:layer_utility:{tenant_id}:{category}"
        if r:
            cached = await r.get(cache_key)
            if cached: return json.loads(cached)
            
        pool = await get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT resolved_at_layer, COUNT(*) * 100.0 / SUM(COUNT(*)) OVER () as pct
                FROM cognitive_executions
                WHERE tenant_id = $1 AND task_category = $2
                  AND created_at > NOW() - INTERVAL '30 days'
                GROUP BY resolved_at_layer
            """, tenant_id, category)
            
            utility = {r['resolved_at_layer']: float(r['pct']) / 100.0 for r in rows}
            if r: await r.setex(cache_key, 3600, json.dumps(utility))
            return utility
