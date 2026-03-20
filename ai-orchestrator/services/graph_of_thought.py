"""
graph_of_thought.py
===================
Graph-of-Thought distribuído — Plano Avançado v2, Parte 2.

Raciocínio estrutural persistido no Neo4j.
Cada problema resolvido vira nó no grafo — reutilizável nas próximas execuções.
Expansão paralela de hipóteses via ThreadPoolExecutor (sem Ray como dep hard).

Schema esperado no Neo4j:
  (:Thought   {id, content, task_type, embedding, confidence})
  (:Hypothesis {id, content, score})
  (:Conclusion {id, content, confidence, verified, steps})
  (:Problem   {id, description, task_type, context_hash})
  (:Problem)-[:SOLVED_BY]->(:Conclusion)
  (:Thought)-[:LEADS_TO]->(:Thought)

Public API
----------
  got = GraphOfThought(neo4j_uri, user, password)
  result = got.find_or_create_reasoning(problem, task_type, embedder_func)
  got.persist_reasoning(problem, task_type, ctx_hash, solution, steps, success)
"""
from __future__ import annotations
from services.streaming.core.config import env_get

import hashlib
import logging
import math
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

log = logging.getLogger("orch.got")

# Minimum vector similarity to reuse existing reasoning
_VECTOR_THRESHOLD   = 0.88
# Max depth for hypothesis expansion
_MAX_EXPANSION_DEPTH = 3
# Number of parallel expansion workers
_N_WORKERS          = 4
# Minimum success_rate to reuse a conclusion
_MIN_SUCCESS_RATE   = 0.75


# ── Types ─────────────────────────────────────────────────────────────────────

@dataclass
class ThoughtNode:
    node_id:       str
    content:       str
    node_type:     str            # "hypothesis" | "action" | "observation" | "conclusion"
    confidence:    float = 0.5
    success_count: int   = 0
    failure_count: int   = 0
    children:      list["ThoughtNode"] = field(default_factory=list, repr=False)

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.failure_count
        return self.success_count / total if total > 0 else 0.5

    @property
    def ucb_score(self) -> float:
        """Upper Confidence Bound — balances exploration vs exploitation."""
        total = self.success_count + self.failure_count
        if total == 0:
            return float("inf")
        exploitation = self.success_rate
        exploration  = math.sqrt(2 * math.log(total + 1) / total)
        return exploitation + 0.5 * exploration


@dataclass
class GoTResult:
    solution:    str
    steps:       list[str]
    source:      str           # "neo4j_existing" | "neo4j_new" | "unavailable"
    confidence:  float
    context_hash: str


# ── GraphOfThought ────────────────────────────────────────────────────────────

class GraphOfThought:
    """
    Distributed Graph-of-Thought backed by Neo4j.

    If Neo4j is unavailable, degrades gracefully (returns None on all lookups).
    """

    def __init__(self, uri: str, user: str, password: str):
        self._uri      = uri
        self._user     = user
        self._password = password
        self._driver   = None
        self._executor = ThreadPoolExecutor(max_workers=_N_WORKERS,
                                            thread_name_prefix="got_worker")

    def _get_driver(self):
        if self._driver is None:
            from neo4j import GraphDatabase
            self._driver = GraphDatabase.driver(
                self._uri, auth=(self._user, self._password)
            )
        return self._driver

    # ── Public API ────────────────────────────────────────────────────────────

    def find_or_create_reasoning(
        self,
        problem: str,
        task_type: str,
        embedder_func: Optional[Callable[[str], list[float]]] = None,
    ) -> Optional[GoTResult]:
        """
        Look up existing reasoning for this problem in Neo4j.
        Returns GoTResult if a confident solution exists, else None
        (caller should proceed to LLM).
        """
        ctx_hash = _context_hash(task_type, problem)

        # 1. Exact context_hash match (fastest)
        result = self._lookup_by_hash(ctx_hash, task_type)
        if result:
            return result

        # 2. Vector similarity (if embedder available)
        if embedder_func:
            try:
                vec = embedder_func(problem)
                result = self._lookup_by_vector(vec, task_type)
                if result:
                    return result
            except Exception as exc:
                log.debug("got_vector_lookup_failed error=%s", exc)

        # 3. Parallel expansion of related thought nodes
        try:
            result = self._expand_parallel(task_type, ctx_hash)
            if result:
                return result
        except Exception as exc:
            log.debug("got_expansion_failed error=%s", exc)

        return None

    def persist_reasoning(
        self,
        problem: str,
        task_type: str,
        solution: str,
        steps: list[str],
        success: bool,
        embedder_func: Optional[Callable[[str], list[float]]] = None,
    ):
        """
        Persist a resolved problem + solution into the Neo4j graph.
        Best-effort — does not raise.
        """
        ctx_hash = _context_hash(task_type, problem)
        embedding: list[float] = []
        if embedder_func:
            try:
                embedding = embedder_func(problem)
            except Exception:
                pass

        try:
            driver = self._get_driver()
            with driver.session() as session:
                session.run(
                    """
                    MERGE (p:Problem {context_hash: $ctx_hash})
                      ON CREATE SET p.description = $problem,
                                    p.task_type   = $task_type,
                                    p.created_at  = datetime()
                    MERGE (c:Conclusion {description: $solution})
                      ON CREATE SET c.steps       = $steps,
                                    c.verified    = $success,
                                    c.confidence  = CASE WHEN $success THEN 0.9 ELSE 0.3 END,
                                    c.created_at  = datetime()
                    MERGE (p)-[r:SOLVED_BY]->(c)
                      ON CREATE SET r.attempts   = 0,
                                    r.successes  = 0,
                                    r.success_rate = 0.5
                    SET r.attempts   = r.attempts + 1,
                        r.successes  = r.successes + CASE WHEN $success THEN 1 ELSE 0 END,
                        r.success_rate = toFloat(r.successes + CASE WHEN $success THEN 1 ELSE 0 END)
                                       / toFloat(r.attempts + 1),
                        r.updated_at = datetime()
                    """,
                    ctx_hash=ctx_hash, problem=problem, task_type=task_type,
                    solution=solution, steps=steps, success=success,
                )

                # Store embedding as Thought node for vector lookup
                if embedding:
                    session.run(
                        """
                        MERGE (t:Thought {context_hash: $ctx_hash})
                          ON CREATE SET t.content   = $problem,
                                        t.task_type = $task_type,
                                        t.embedding = $embedding,
                                        t.confidence = 0.8,
                                        t.created_at = datetime()
                        SET t.embedding = $embedding
                        """,
                        ctx_hash=ctx_hash, problem=problem,
                        task_type=task_type, embedding=embedding,
                    )
        except Exception as exc:
            log.debug("got_persist_error error=%s", exc)

    def close(self):
        if self._driver:
            self._driver.close()
        self._executor.shutdown(wait=False)

    # ── Internal lookup methods ───────────────────────────────────────────────

    def _lookup_by_hash(self, ctx_hash: str, task_type: str) -> Optional[GoTResult]:
        try:
            driver = self._get_driver()
            with driver.session() as session:
                result = session.run(
                    """
                    MATCH (p:Problem {context_hash: $ctx_hash})-[r:SOLVED_BY]->(c:Conclusion)
                    WHERE r.success_rate >= $min_rate
                    RETURN c.description AS solution,
                           c.steps       AS steps,
                           r.success_rate AS confidence
                    ORDER BY r.success_rate DESC
                    LIMIT 1
                    """,
                    ctx_hash=ctx_hash, min_rate=_MIN_SUCCESS_RATE,
                )
                record = result.single()
                if record:
                    return GoTResult(
                        solution     = record["solution"] or "",
                        steps        = list(record["steps"] or []),
                        source       = "neo4j_existing",
                        confidence   = float(record["confidence"]),
                        context_hash = ctx_hash,
                    )
        except Exception as exc:
            log.debug("got_hash_lookup_error error=%s", exc)
        return None

    def _lookup_by_vector(self, vector: list[float],
                           task_type: str) -> Optional[GoTResult]:
        """Use Neo4j vector index if available (Neo4j 5.x)."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                result = session.run(
                    """
                    CALL db.index.vector.queryNodes(
                        'thought_embedding_idx', 3, $vector
                    ) YIELD node, score
                    WHERE score >= $threshold AND node.task_type = $task_type
                    WITH node, score ORDER BY score DESC LIMIT 1
                    MATCH (p:Problem {context_hash: node.context_hash})-[r:SOLVED_BY]->(c:Conclusion)
                    WHERE r.success_rate >= $min_rate
                    RETURN c.description AS solution,
                           c.steps       AS steps,
                           r.success_rate * score AS confidence,
                           node.context_hash AS ctx_hash
                    LIMIT 1
                    """,
                    vector=vector, threshold=_VECTOR_THRESHOLD,
                    task_type=task_type, min_rate=_MIN_SUCCESS_RATE,
                )
                record = result.single()
                if record:
                    return GoTResult(
                        solution     = record["solution"] or "",
                        steps        = list(record["steps"] or []),
                        source       = "neo4j_existing",
                        confidence   = float(record["confidence"]),
                        context_hash = record["ctx_hash"],
                    )
        except Exception as exc:
            # Vector index may not be set up — not an error
            log.debug("got_vector_index_unavailable error=%s", exc)
        return None

    def _expand_parallel(self, task_type: str,
                          ctx_hash: str) -> Optional[GoTResult]:
        """
        Expand related Thought nodes in parallel threads.
        Returns the best hypothesis found, or None.
        """
        try:
            driver = self._get_driver()
            # Find root thought nodes with same task_type (up to N_WORKERS)
            with driver.session() as session:
                result = session.run(
                    """
                    MATCH (t:Thought {task_type: $task_type})
                    WHERE t.confidence >= 0.7
                    RETURN t.context_hash AS hash, t.confidence AS conf
                    ORDER BY t.confidence DESC
                    LIMIT $n
                    """,
                    task_type=task_type, n=_N_WORKERS,
                )
                roots = [dict(r) for r in result]

            if not roots:
                return None

            futures = {
                self._executor.submit(
                    self._expand_single, r["hash"], task_type
                ): r
                for r in roots
            }

            best: Optional[GoTResult] = None
            for future in as_completed(futures, timeout=3.0):
                try:
                    candidate = future.result()
                    if candidate and (
                        best is None or candidate.confidence > best.confidence
                    ):
                        best = candidate
                except Exception:
                    pass

            return best if (best and best.confidence >= _MIN_SUCCESS_RATE) else None
        except Exception as exc:
            log.debug("got_expand_parallel_error error=%s", exc)
            return None

    def _expand_single(self, root_hash: str,
                        task_type: str) -> Optional[GoTResult]:
        """Expand one thought subtree (runs in thread pool)."""
        try:
            driver = self._get_driver()
            with driver.session() as session:
                result = session.run(
                    """
                    MATCH (root:Thought {context_hash: $hash})-[:LEADS_TO*1..$depth]->(leaf)
                    WHERE NOT (leaf)-[:LEADS_TO]->()
                    OPTIONAL MATCH (p:Problem {context_hash: leaf.context_hash})-[r:SOLVED_BY]->(c:Conclusion)
                    WHERE r.success_rate >= $min_rate
                    RETURN c.description AS solution,
                           c.steps       AS steps,
                           COALESCE(r.success_rate, 0.0) * leaf.confidence AS confidence,
                           leaf.context_hash AS ctx_hash
                    ORDER BY confidence DESC
                    LIMIT 1
                    """,
                    hash=root_hash, depth=_MAX_EXPANSION_DEPTH,
                    min_rate=_MIN_SUCCESS_RATE,
                )
                record = result.single()
                if record and record["solution"]:
                    return GoTResult(
                        solution     = record["solution"],
                        steps        = list(record["steps"] or []),
                        source       = "neo4j_existing",
                        confidence   = float(record["confidence"]),
                        context_hash = record["ctx_hash"] or root_hash,
                    )
        except Exception as exc:
            log.debug("got_expand_single_error root=%s error=%s", root_hash, exc)
        return None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _context_hash(task_type: str, description: str) -> str:
    payload = f"{task_type}:{description[:200].lower().strip()}"
    return hashlib.md5(payload.encode()).hexdigest()[:16]


# ── Singleton ─────────────────────────────────────────────────────────────────

_instance: Optional[GraphOfThought] = None


def get_got() -> Optional[GraphOfThought]:
    global _instance
    if _instance is None:
        import os
        uri  = env_get("NEO4J_URI", default="bolt://localhost:7687")
        user = env_get("NEO4J_USER", default="neo4j")
        pwd  = env_get("NEO4J_PASSWORD", default="neo4j")
        try:
            _instance = GraphOfThought(uri, user, pwd)
        except Exception as exc:
            log.debug("got_init_failed error=%s", exc)
    return _instance
