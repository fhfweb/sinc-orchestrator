"""
memory_layers.py
================
Implementation of the Cognitive Memory Hierarchy (Phase 1: L1 & L2).
Based on the Cognitive Runtime Orchestrator — Plano Avançado v2.
"""

import os
import json
import hashlib
import asyncio
import logging
import re
from typing import Any, Optional, Dict, List, Callable
from datetime import datetime
from collections import deque

log = logging.getLogger("orch.memory")

# ──────────────────────────────────────────────
# L0 — Rule Engine (Deterministic Overrides)
# ──────────────────────────────────────────────

class L0RuleEngine:
    """
    Nível 0: Regras determinísticas de alta velocidade.
    Útil para comandos administrativos ou padrões fixos conhecidos.
    """
    def __init__(self):
        self.rules = [
            {
                "regex": r"(?i)ping",
                "result": {"solution": "pong", "source": "L0_rule_engine"}
            },
            {
                "regex": r"(?i)uptime|status",
                "result": {"solution": "Orchestrator is operational.", "source": "L0_rule_engine"}
            }
        ]

    def check(self, description: str) -> Optional[Dict]:
        for rule in self.rules:
            if re.search(rule["regex"], description):
                return rule["result"]
        return None

# ──────────────────────────────────────────────
# L1 — Deterministic Cache (Redis + In-Memory)
# ──────────────────────────────────────────────

class L1DeterministicCache:
    """
    Cache de nível 1: extremamente rápido.
    Chave = hash determinístico do input normalizado.
    """
    TTL_BY_TYPE = {
        "create_route":    86400 * 7,   # 7 dias
        "create_test":     86400 * 3,   # 3 dias
        "generate_schema": 86400 * 1,   # 1 dia
        "fix_bug":         3600,         # 1 hora
        "analyze_impact":  1800,         # 30 min
    }

    def __init__(self, redis_url: str = "redis://localhost:6379"):
        try:
            import redis.asyncio as redis
            self.redis = redis.from_url(redis_url, decode_responses=True)
        except Exception:
            self.redis = None
            log.warning("redis_unavailable_fallback_to_memory_only")
        
        self._local: Dict[str, Any] = {}  # L1.5: dict in-memory para hot path

    def _make_key(self, task_type: str, normalized_input: str) -> str:
        payload = f"{task_type}:{normalized_input.lower().strip()}"
        return f"orch:l1:{hashlib.sha256(payload.encode()).hexdigest()[:32]}"

    def _normalize(self, description: str) -> str:
        """Remove variações irrelevantes — aumenta hit rate."""
        import re
        text = description.lower()
        # Remove stop words comuns em português
        text = re.sub(r"\b(o|a|os|as|um|uma|de|do|da|para|com|em)\b", "", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    async def get(self, task_type: str, description: str) -> Optional[Dict]:
        key = self._make_key(task_type, self._normalize(description))

        # L1.5: dict in-memory (hot path, ~0ms)
        if key in self._local:
            return {**self._local[key], "cache_level": "L1.5_memory"}

        # L1: Redis (~2ms)
        if self.redis:
            try:
                raw = await self.redis.get(key)
                if raw:
                    data = json.loads(raw)
                    self._local[key] = data  # promove para L1.5
                    return {**data, "cache_level": "L1_redis"}
            except Exception as e:
                log.error("l1_redis_get_error", error=str(e))

        return None

    async def set(self, task_type: str, description: str, result: Dict):
        key = self._make_key(task_type, self._normalize(description))
        ttl = self.TTL_BY_TYPE.get(task_type, 3600)
        
        # In-memory first
        self._local[key] = result
        
        # Then Redis
        if self.redis:
            try:
                payload = json.dumps(result)
                await self.redis.setex(key, ttl, payload)
            except Exception as e:
                log.error("l1_redis_set_error", error=str(e))

# ──────────────────────────────────────────────
# L2 — Semantic Memory (Qdrant)
# ──────────────────────────────────────────────

class L2SemanticMemory:
    """
    Nível 2: similaridade semântica com threshold adaptativo.
    """
    BASE_THRESHOLD = 0.92
    MIN_THRESHOLD  = 0.85
    MAX_THRESHOLD  = 0.97

    def __init__(self, qdrant_host: str, qdrant_port: int, embedder_func):
        self.qdrant_url = f"http://{qdrant_host}:{qdrant_port}"
        self.embedder_func = embedder_func
        self._threshold = self.BASE_THRESHOLD
        
        # Phase 15: Initialize Sparse Embedder for Hybrid Search
        try:
            from fastembed import SparseTextEmbedding
            self.sparse_embedder = SparseTextEmbedding(model_name="prithivida/Splade_PP_en_v1")
            log.info("l2_sparse_embedder_ready")
        except Exception as exc:
            self.sparse_embedder = None
            log.warning("l2_sparse_embedder_failed_falling_back_to_dense_only error=%s", exc)

    def adjust_threshold(self, success_rate: float):
        """Auto-tuning do threshold baseado na performance."""
        if success_rate > 0.95:
            self._threshold = min(self._threshold + 0.01, self.MAX_THRESHOLD)
        elif success_rate < 0.80:
            self._threshold = max(self._threshold - 0.01, self.MIN_THRESHOLD)
        log.info("l2_threshold_adjusted", threshold=self._threshold)

    async def search(self, description: str, task_type: str, project_id: str, tenant_id: str) -> Optional[Dict]:
        from services.streaming.core.config import MEMORY_L2_TIMEOUT_S
        
        async def _run():
            dense_vector = self.embedder_func(description)
            if not dense_vector:
                return None

            # Phase 15: Sparse vector for technical precision
            sparse_vector = None
            if self.sparse_embedder:
                try:
                    sparse_vector = list(self.sparse_embedder.embed([description]))[0]
                except Exception as exc:
                    log.debug("l2_sparse_vector_failed error=%s", exc)

            collection = f"{tenant_id}_{project_id}_solutions"
            from services.context_retriever import _qdrant_search
            
            filters = {"must": [{"key": "task_type", "match": {"value": task_type}}]}
            
            # Hybrid search if sparse is available
            hits = _qdrant_search(
                collection, 
                dense_vector, 
                top_k=1, 
                filters=filters,
                sparse_vector=sparse_vector if sparse_vector else None
            )
            
            if hits:
                hit = hits[0]
                score = hit.get("score", 0.0)
                if score >= self._threshold:
                    payload = hit.get("payload", {})
                    return {
                        "solution":     payload.get("solution"),
                        "confidence":   score,
                        "cache_level":  "L2_hybrid_semantic",
                        "llm_used":     False,
                        "tokens_saved": payload.get("original_tokens", 0)
                    }
            return None

        try:
            # Wrap standard sync-ish search in thread + timeout
            return await asyncio.wait_for(asyncio.to_thread(_run), timeout=MEMORY_L2_TIMEOUT_S)
        except (Exception, asyncio.TimeoutError) as e:
            log.warning("l2_qdrant_unavailable_skipping error=%s", e)
            return None
# ──────────────────────────────────────────────
# L3 — Graph Reasoning (Neo4j)
# ──────────────────────────────────────────────

# Module-level Neo4j driver singleton — close sessions, never close the driver.
# Prevents per-query connection explosion (audit finding C4).
_neo4j_driver: "Any" = None


def _get_neo4j_driver(uri: str, user: str, password: str) -> "Any":
    global _neo4j_driver
    if _neo4j_driver is None:
        try:
            from neo4j import GraphDatabase
            _neo4j_driver = GraphDatabase.driver(
                uri,
                auth=(user, password),
                max_connection_pool_size=10,
            )
        except Exception as exc:
            log.error("neo4j_driver_init_failed error=%s", exc)
            raise
    return _neo4j_driver


class L3GraphReasoning:
    """
    Nível 3: Raciocínio baseado em vizinhança de grafo.
    Busca se tarefas similares no mesmo contexto já foram resolvidas.
    Uses a module-level driver singleton to avoid per-query connection churn.
    """
    def __init__(self, uri: str, user: str, password: str):
        self.uri = uri
        self.user = user
        self.password = password

    async def search(self, description: str, task_type: str, project_id: str, tenant_id: str) -> Optional[Dict]:
        from services.streaming.core.config import MEMORY_L3_TIMEOUT_S

        def _run_neo4j():
            try:
                driver = _get_neo4j_driver(self.uri, self.user, self.password)
                with driver.session() as session:
                    result = session.run("""
                        MATCH (t:Task {description: $desc, type: $type})
                        WHERE t.project_id = $pid AND t.tenant_id = $tid AND t.status = 'done'
                        RETURN t.solution AS solution, t.tokens_used AS tokens
                        LIMIT 1
                    """, desc=description, type=task_type, pid=project_id, tid=tenant_id)
                    row = result.single()
                if row:
                    return {
                        "solution":     row["solution"],
                        "cache_level":  "L3_graph",
                        "llm_used":     False,
                        "tokens_saved": row["tokens"] or 0,
                    }
            except Exception as exc:
                log.error("l3_graph_search_error error=%s", exc)
                raise
            return None

        try:
            return await asyncio.wait_for(asyncio.to_thread(_run_neo4j), timeout=MEMORY_L3_TIMEOUT_S)
        except (Exception, asyncio.TimeoutError) as e:
            log.warning("l3_neo4j_unavailable_skipping error=%s", e)
            return None

# ──────────────────────────────────────────────
# L4 — Event Memory (PostgreSQL Execution History)
# ──────────────────────────────────────────────

class L4EventMemory:
    """
    Nível 4: Memória de longo prazo baseada no histórico de eventos.
    Identifica cadeias de execução (llm_request_finished) recorrentes.
    """
    def __init__(self, db_params: Dict):
        self.db_params = db_params

    async def search(self, description: str, project_id: str, tenant_id: str) -> Optional[Dict]:
        try:
            from services.streaming.core.db import async_db
            async with async_db(bypass_rls=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        SELECT payload->>'response' as response, 
                               (payload->>'input_tokens')::int + (payload->>'output_tokens')::int as tokens
                        FROM agent_events
                        WHERE project_id = %s AND tenant_id = %s
                          AND event_type = 'llm_request_finished'
                          AND payload->>'prompt' ILIKE %s
                        ORDER BY created_at DESC
                        LIMIT 1
                    """, (project_id, tenant_id, f"%{description}%"))
                    row = await cur.fetchone()
            
            if row:
                return {
                    "solution":     row["response"],
                    "cache_level":  "L4_event_memory",
                    "llm_used":     False,
                    "tokens_saved": row["tokens"] or 0
                }
        except Exception as e:
            log.error("l4_event_memory_search_error", error=str(e))
        return None

# ──────────────────────────────────────────────
# Memory Hierarchy Router
# ──────────────────────────────────────────────

class MemoryHierarchyRouter:
    """
    Coordena L0→L1→L2→L3→L4. Retorna na primeira resposta válida.
    """
    def __init__(self, 
                 l0: L0RuleEngine,
                 l1: L1DeterministicCache, 
                 l2: L2SemanticMemory,
                 l3: Optional[L3GraphReasoning] = None,
                 l4: Optional[L4EventMemory] = None):
        self.l0 = l0
        self.l1 = l1
        self.l2 = l2
        self.l3 = l3
        self.l4 = l4

    async def resolve(self, task_type: str, description: str, project_id: str, tenant_id: str) -> Optional[Dict]:
        # L0: Regras Dinâmicas (Ultra-Rápido)
        hit = self.l0.check(description)
        if hit:
            return {**hit, "cache_level": "L0_rules"}

        # L1: Determinístico (Fast Cache)
        hit = await self.l1.get(task_type, description)
        if hit:
            return hit

        # L2: Semântico (Async + Timeout)
        hit = await self.l2.search(description, task_type, project_id, tenant_id)
        if hit:
            await self.l1.set(task_type, description, hit)
            return hit

        # L3: Grafo (Opcional, Async + Timeout)
        if self.l3:
            hit = await self.l3.search(description, task_type, project_id, tenant_id)
            if hit:
                await self.l1.set(task_type, description, hit)
                return hit

        # L4: Eventos (Opcional, Async)
        if self.l4:
            hit = await self.l4.search(description, project_id, tenant_id)
            if hit:
                await self.l1.set(task_type, description, hit)
                return hit

        return None

    async def learn(self, task_type: str, description: str, solution: str, 
              project_id: str, tenant_id: str, success: bool, tokens_used: int = 0):
        """
        Aprende com o resultado — persiste em todos os níveis em background.
        """
        result = {
            "solution": solution,
            "llm_used": True,
            "tokens_used": tokens_used
        }
        
        # Persiste em L1 (Redis) - Async
        await self.l1.set(task_type, description, result)
        
        # Persiste em L2 (Vector Store) - Background IO
        def _learn_l2():
            try:
                from services.context_retriever import ContextRetriever
                r = ContextRetriever()
                r.store_solution(
                    query=description, answer=solution,
                    project_id=project_id, tenant_id=tenant_id, intent=task_type
                )
            except Exception as exc:  # audit C1: never drop learning errors silently
                log.error("l2_learn_failed error=%s project=%s tenant=%s", exc, project_id, tenant_id)

        # L3: Atualiza Grafo (Neo4j) - Background IO — uses singleton driver (audit C4)
        def _learn_l3():
            if self.l3:
                try:
                    driver = _get_neo4j_driver(self.l3.uri, self.l3.user, self.l3.password)
                    with driver.session() as session:
                        session.run("""
                            MERGE (t:Task {description: $desc, type: $type, project_id: $pid, tenant_id: $tid})
                            SET t.solution = $sol, t.tokens_used = $tokens, t.status = 'done', t.updated_at = datetime()
                        """, desc=description, type=task_type, pid=project_id, tid=tenant_id, sol=solution, tokens=tokens_used)
                except Exception as exc:  # audit C1: never drop learning errors silently
                    log.error("l3_learn_failed error=%s project=%s tenant=%s", exc, project_id, tenant_id)

        await asyncio.gather(
            asyncio.to_thread(_learn_l2),
            asyncio.to_thread(_learn_l3)
        )
