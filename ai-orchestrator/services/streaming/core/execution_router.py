import json
import logging
import hashlib
from enum import Enum
from typing import Optional, Tuple
from .redis_ import get_async_redis

log = logging.getLogger("orchestrator")

class ExecutionPath(Enum):
    INSTANT  = "instant"   # L0 cache hit → resposta em <50ms
    FAST     = "fast"      # sem simulação, sem GCS completo → <300ms
    STANDARD = "standard"  # GCS + decisões, sem simulação → <800ms
    DEEP     = "deep"      # GCS + simulação + decision budget completo → <2000ms

# Categorias pré-definidas para facilitação de roteamento
LOW_RISK_CATEGORIES = {"info", "read_only", "status_check", "ping"}
HIGH_RISK_CATEGORIES = {"deployment", "database_migration", "security_audit", "destructive"}

def hash_task(title: str, category: str) -> str:
    payload = f"{title}:{category.lower().strip()}"
    return hashlib.sha256(payload.encode()).hexdigest()[:16]

async def route_execution(
    task_title: str,
    task_category: str,
    tenant_id: str,
    assigned_agent: Optional[str] = None,
    depends_on_count: int = 0,
    has_primary_file: bool = False
) -> Tuple[ExecutionPath, str]:
    """
    Decide o caminho de execução em <50ms.
    Usa apenas sinais que já estão em cache (Redis).
    """
    redis = get_async_redis()
    if not redis:
        return ExecutionPath.STANDARD, "redis_unavailable_fallback"

    # ── INSTANT: cache L0 hit ─────────────────────────────────────────────────
    cache_key = f"l0:{tenant_id}:{hash_task(task_title, task_category)}"
    try:
        l0_hit = await redis.get(cache_key)
        if l0_hit:
            return ExecutionPath.INSTANT, "l0_cache_hit"
    except Exception:
        pass

    # ── Sinais rápidos do Redis (sem DB) ──────────────────────────────────────
    try:
        category_stats_raw = await redis.get(f"sinc:category_stats:{tenant_id}:{task_category}")
        agent_score_raw = await redis.zscore(
            f"sinc:leaderboard:{tenant_id}:{task_category}",
            assigned_agent or "auto"
        )
        recent_failures_count = await redis.get(
            f"sinc:recent_failures:{tenant_id}:{task_category}"
        )
    except Exception as e:
        log.warning(f"router_signals_error: {e}")
        return ExecutionPath.STANDARD, "error_fetching_signals"

    category_stats = json.loads(category_stats_raw) if category_stats_raw else {}
    agent_score = float(agent_score_raw) if agent_score_raw else 0.5
    recent_failures = int(recent_failures_count or 0)

    # Sinais de FAST PATH (tudo favorável, sem simulação necessária)
    fast_path_signals = [
        category_stats.get("success_rate", 0) >= 0.80,   # categoria tem bom histórico
        agent_score >= 0.75,                               # agente está performando bem
        recent_failures < 2,                               # sem falhas recentes
        task_category in LOW_RISK_CATEGORIES,              # categoria de baixo risco
        not has_primary_file,                              # não envolve arquivo específico
    ]

    # Sinais de DEEP PATH (algo preocupante, simulação necessária)
    deep_path_signals = [
        category_stats.get("success_rate", 1) < 0.50,     # categoria problemática
        recent_failures >= 3,                              # padrão de falha recente
        task_category in HIGH_RISK_CATEGORIES,             # categoria de alto risco
        depends_on_count > 5,                              # muitas dependências
    ]

    fast_count = sum(fast_path_signals)
    deep_count = sum(deep_path_signals)

    if deep_count >= 2:
        return ExecutionPath.DEEP, f"{deep_count} sinais de alto risco detectados"

    if fast_count >= 3:
        return ExecutionPath.FAST, f"{fast_count}/5 sinais favoráveis"

    return ExecutionPath.STANDARD, "perfil misto — caminho padrão"
