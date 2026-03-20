from dataclasses import dataclass
from enum import Enum
import json
import logging
from typing import Optional

log = logging.getLogger("orch.intelligence.router")

class IntelligenceDepth(Enum):
    INSTANT   = "instant"    # L0 apenas — cache hit esperado
    LIGHT     = "light"      # L0 + L1 — regras determinísticas
    STANDARD  = "standard"   # L0 + L1 + L3 — semântico sem grafo
    DEEP      = "deep"       # L0 + L1 + L2 + L3 — grafo + semântico
    MAXIMUM   = "maximum"    # tudo + LLM synthesis + quality gate + loops

@dataclass
class IntelligenceStrategy:
    depth: IntelligenceDepth
    use_neo4j: bool
    use_qdrant: bool
    use_prediction: bool
    use_proactive_injection: bool
    max_retrieval_ms: int
    confidence_threshold: float
    auto_decompose_if_low_confidence: bool
    require_impact_assessment: bool
    reasoning: str

async def classify_task_intelligence(description: str, task_type: str, tenant_id: str, primary_file: Optional[str] = None) -> IntelligenceStrategy:
    """
    Determina a estratégia de inteligência baseada em:
    - Complexidade estimada da task
    - Histórico de tasks similares
    - Custo de estar errado (blast radius)
    - Custo de processamento
    """
    from services.streaming.core.redis_ import get_async_redis
    redis = get_async_redis()
    
    # Sinais rápidos — não bloqueiam, vêm de cache
    category_history = await redis.get(f"sinc:category_stats:{tenant_id}:{task_type}") if redis else None
    has_primary_file = bool(primary_file)
    word_count = len(description.split())
    
    # Scoring de complexidade (0.0 → 1.0)
    complexity_score = 0.0

    # Sinal 1: tamanho da descrição é proxy de complexidade
    complexity_score += min(word_count / 200, 0.25)

    # Sinal 2: tasks com arquivo primário envolvem código real
    if has_primary_file:
        complexity_score += 0.20

    # Sinal 3: histórico de categorias
    HIGH_COMPLEXITY_CATEGORIES = {
        "refactor", "architecture", "security", "migration",
        "performance", "debugging", "integration", "goal"
    }
    LOW_COMPLEXITY_CATEGORIES = {
        "docs", "typo", "rename", "format", "comment", "style"
    }
    
    if task_type in HIGH_COMPLEXITY_CATEGORIES:
        complexity_score += 0.30
    elif task_type in LOW_COMPLEXITY_CATEGORIES:
        complexity_score -= 0.20

    # Sinal 4: histórico de success_rate na categoria
    if category_history:
        try:
            stats = json.loads(category_history)
            if stats.get("success_rate", 1.0) < 0.6:
                complexity_score += 0.20
        except Exception: pass

    complexity_score = max(0.0, min(1.0, complexity_score))

    # Mapear score para estratégia
    if complexity_score < 0.15:
        return IntelligenceStrategy(
            depth=IntelligenceDepth.INSTANT,
            use_neo4j=False, use_qdrant=False, use_prediction=False,
            use_proactive_injection=False, max_retrieval_ms=50,
            confidence_threshold=0.95, auto_decompose_if_low_confidence=False,
            require_impact_assessment=False,
            reasoning=f"baixa complexidade estimada ({complexity_score:.2f}) — cache first"
        )
    elif complexity_score < 0.35:
        return IntelligenceStrategy(
            depth=IntelligenceDepth.LIGHT,
            use_neo4j=False, use_qdrant=True, use_prediction=False,
            use_proactive_injection=True, max_retrieval_ms=200,
            confidence_threshold=0.85, auto_decompose_if_low_confidence=False,
            require_impact_assessment=False,
            reasoning=f"complexidade leve ({complexity_score:.2f}) — semântico básico"
        )
    elif complexity_score < 0.60:
        return IntelligenceStrategy(
            depth=IntelligenceDepth.STANDARD,
            use_neo4j=False, use_qdrant=True, use_prediction=True,
            use_proactive_injection=True, max_retrieval_ms=400,
            confidence_threshold=0.75, auto_decompose_if_low_confidence=True,
            require_impact_assessment=has_primary_file,
            reasoning=f"complexidade média ({complexity_score:.2f}) — semântico completo"
        )
    elif complexity_score < 0.80:
        return IntelligenceStrategy(
            depth=IntelligenceDepth.DEEP,
            use_neo4j=True, use_qdrant=True, use_prediction=True,
            use_proactive_injection=True, max_retrieval_ms=600,
            confidence_threshold=0.70, auto_decompose_if_low_confidence=True,
            require_impact_assessment=True,
            reasoning=f"alta complexidade ({complexity_score:.2f}) — grafo + semântico"
        )
    else:
        return IntelligenceStrategy(
            depth=IntelligenceDepth.MAXIMUM,
            use_neo4j=True, use_qdrant=True, use_prediction=True,
            use_proactive_injection=True, max_retrieval_ms=1000,
            confidence_threshold=0.65, auto_decompose_if_low_confidence=True,
            require_impact_assessment=True,
            reasoning=f"complexidade máxima ({complexity_score:.2f}) — pipeline completo"
        )
