from services.streaming.core.config import env_get
"""
Context Retriever
=================
Builds rich context for the /ask endpoint by combining:
  1. Qdrant semantic search  — top-K most relevant code chunks
  2. Neo4j graph expansion   — classes/functions related to those files
  3. File snippet loading    — actual source lines around each hit

Usage:
    from services.context_retriever import ContextRetriever
    retriever = ContextRetriever()
    ctx = retriever.retrieve(
        query="How does authentication work?",
        project_id="sinc",
        tenant_id="local",
        top_k=8,
    )
    # ctx = {
    #   "chunks":  [{"file": ..., "chunk": ..., "text": ..., "score": ...}],
    #   "graph":   [{"type": "Class", "name": ..., "file": ..., "line": ...}],
    #   "context": "<formatted string ready to inject into LLM prompt>",
    # }
"""

import asyncio
import os
import json
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

from services.semantic_backend import (
    embed_text as _shared_embed_text,
    ensure_collection as _shared_ensure_collection,
    search_points as _shared_search_points,
    scroll_points as _shared_scroll_points,
    upsert_point as _shared_upsert_point,
)


def _now_iso() -> str:
    return datetime.now().isoformat()


# ──────────────────────────────────────────────
# CONFIGURATION
# ──────────────────────────────────────────────

NEO4J_URI  = env_get("NEO4J_URI", default="bolt://localhost:7687")
NEO4J_USER = env_get("NEO4J_USER", default="neo4j")
NEO4J_PASS = env_get("NEO4J_PASS", default=env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/")[-1])

EMBED_MODEL  = env_get("OLLAMA_EMBED_MODEL", default="nomic-embed-text")

DEFAULT_TOP_K      = int(env_get("CONTEXT_TOP_K", default="8"))
DEFAULT_GRAPH_HOPS = int(env_get("CONTEXT_GRAPH_HOPS", default="1"))
SNIPPET_LINES      = int(env_get("CONTEXT_SNIPPET_LINES", default="30"))


# ──────────────────────────────────────────────
# EMBEDDING (same as ingest_pipeline)
# ──────────────────────────────────────────────

def _embed_query(text: str) -> list[float]:
    vector, error = _shared_embed_text(text, model=EMBED_MODEL, timeout=30)
    if error:
        print(f"[context] embed error: {error}")
        return []
    return vector


# ──────────────────────────────────────────────
# QDRANT SEARCH
# ──────────────────────────────────────────────

def _qdrant_collection(project_id: str, tenant_id: str) -> str:
    return f"{tenant_id}_{project_id}_code"


def _solutions_collection(project_id: str, tenant_id: str) -> str:
    return f"{tenant_id}_{project_id}_solutions"


def _ensure_collection(collection: str, dim: int):
    """Create a Qdrant collection if it doesn't already exist."""
    error = _shared_ensure_collection(collection, dim, timeout=10)
    if error:
        print(f"[context] create collection error: {error}")


def _qdrant_search(collection: str, vector: list[float], top_k: int,
                   filters: Optional[dict] = None,
                   sparse_vector: Optional[dict] = None) -> list[dict]:
    """Returns list of {id, score, payload}."""
    hits, error = _shared_search_points(
        collection,
        vector,
        top_k,
        filters=filters,
        sparse_vector=sparse_vector,
        timeout=15,
    )
    if error:
        print(f"[context] Qdrant error: {error}")
        return []
    return hits


# ──────────────────────────────────────────────
# NEO4J GRAPH EXPANSION
# ──────────────────────────────────────────────

def _neo4j_expand(file_paths: list[str], project_id: str, tenant_id: str,
                  hops: int = 1) -> list[dict]:
    """
    Given a list of file relative paths, return nearby graph nodes
    (classes, functions, their dependencies) up to `hops` away.
    Returns list of {type, name, file, line, relation}.
    """
    if not file_paths:
        return []

    try:
        from neo4j import GraphDatabase
    except ImportError:
        return []

    nodes = []
    try:
        driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
        with driver.session() as session:
            # Symbols defined in matching files
            result = session.run("""
                MATCH (f:File)-[:DEFINES]->(s)
                WHERE f.path IN $paths
                  AND f.project_id = $pid
                  AND f.tenant_id  = $tid
                RETURN labels(s)[0] AS type, s.name AS name,
                       f.path AS file, s.line AS line
                LIMIT 100
            """, paths=file_paths, pid=project_id, tid=tenant_id)
            for rec in result:
                nodes.append({
                    "type":     rec["type"] or "Symbol",
                    "name":     rec["name"],
                    "file":     rec["file"],
                    "line":     rec["line"] or 0,
                    "relation": "defined_in",
                })

            if hops >= 1 and nodes:
                # Classes extended/implemented by those we found
                names = [n["name"] for n in nodes if n["type"] == "Class"]
                if names:
                    result2 = session.run("""
                        MATCH (c:Class)-[:EXTENDS|IMPLEMENTS]->(p:Class)
                        WHERE c.name IN $names
                          AND c.project_id = $pid
                        RETURN 'Class' AS type, p.name AS name,
                               p.file  AS file, p.line AS line, 'extends' AS relation
                        LIMIT 50
                    """, names=names, pid=project_id)
                    for rec in result2:
                        nodes.append({
                            "type":     "Class",
                            "name":     rec["name"],
                            "file":     rec["file"],
                            "line":     rec["line"] or 0,
                            "relation": rec["relation"],
                        })

        driver.close()
    except Exception as exc:
        print(f"[context] Neo4j error: {exc}")



# ──────────────────────────────────────────────
# FILE SNIPPET LOADER
# ──────────────────────────────────────────────

def _load_snippet(project_path: Optional[str], rel_file: str, line: int,
                  window: int = SNIPPET_LINES) -> str:
    """Load `window` lines centered on `line` from a file."""
    if not project_path:
        return ""
    abs_path = Path(project_path) / rel_file
    if not abs_path.exists():
        return ""
    try:
        lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
        start = max(0, line - window // 2)
        end   = min(len(lines), start + window)
        snippet_lines = lines[start:end]
        numbered = "\n".join(f"{start+i+1}: {l}" for i, l in enumerate(snippet_lines))
        return numbered
    except Exception:
        return ""


# ──────────────────────────────────────────────
# CONTEXT FORMATTER
# ──────────────────────────────────────────────

async def _format_context_cognitive(
    chunks: list[dict], 
    graph_nodes: list[dict],
    project_id: str,
    tenant_id: str,
    agent_name: str = "orchestrator",
    description: str = ""
) -> str:
    """Build a formatted context string using the Level 5 ContextEngine."""
    from services.context_engine import get_context_engine
    engine = get_context_engine()
    
    # 1. Structure signals for the Memory Active engine
    signal_data = {
        "code_chunk": chunks,
        "structural_node": graph_nodes,
        "past_solution": [], # Higher-level orchestrators inject these via build_active_context directly
        "error_pitfall": []
    }
    
    return await engine.build_active_context(
        task_description=description,
        signal_data=signal_data
    )


def _format_context_legacy(chunks: list[dict], graph_nodes: list[dict]) -> str:
    """Fallback legacy formatter."""
    parts = []
    if chunks:
        parts.append("## Relevant Code Chunks\n")
        for i, hit in enumerate(chunks[:6], 1):
            parts.append(f"### [{i}] {hit.get('file')} (line {hit.get('line', 0)})\n```\n{hit.get('text', '')}\n```\n")
    if graph_nodes:
        parts.append("## Knowledge Graph Context\n")
        for node in graph_nodes[:15]:
            parts.append(f"- `{node.get('type')}` **{node.get('name')}** ({node.get('file')}:{node.get('line')})")
    return "\n".join(parts)


# ──────────────────────────────────────────────
# MAIN RETRIEVER CLASS
# ──────────────────────────────────────────────

class ContextRetriever:
    """
    Retrieves multi-modal context (vector + graph) for a natural-language query.
    """

    def __init__(self,
                 project_path: Optional[str] = None,
                 top_k: int = DEFAULT_TOP_K,
                 graph_hops: int = DEFAULT_GRAPH_HOPS):
        self.project_path = project_path
        self.top_k        = top_k
        self.graph_hops   = graph_hops

    def retrieve(self, query: str, project_id: str, tenant_id: str,
                 top_k: Optional[int] = None) -> dict:
        """
        Retrieve context for a query.

        Returns:
            {
                "chunks":  list of semantic hits with score,
                "graph":   list of graph nodes related to those files,
                "context": formatted string for LLM prompt injection,
                "sources": deduplicated list of source file paths,
            }
        """
        k          = top_k or self.top_k
        collection = _qdrant_collection(project_id, tenant_id)

        # 1. Embed the query
        vector = _embed_query(query)

        # 2. Qdrant semantic search
        raw_hits  = _qdrant_search(collection, vector, k)
        chunks    = []
        file_hits = set()
        for hit in raw_hits:
            pl    = hit.get("payload", {})
            chunk = {
                "file":       pl.get("file", ""),
                "chunk":      pl.get("chunk", 0),
                "text":       pl.get("text", ""),
                "score":      round(hit.get("score", 0.0), 4),
                "line":       pl.get("line", 0),
            }
            chunks.append(chunk)
            if chunk["file"]:
                file_hits.add(chunk["file"])

        # 3. Neo4j graph expansion from matched files
        graph_nodes = _neo4j_expand(
            list(file_hits), project_id, tenant_id, hops=self.graph_hops
        )

        # 4. Format context string
        context = _format_context_legacy(chunks, graph_nodes)
        sources = sorted(file_hits)

        return {
            "chunks":  chunks,
            "graph":   graph_nodes or [],
            "context": context,
            "sources": sources,
        }

    def check_semantic_cache(self, query: str, project_id: str, tenant_id: str,
                              threshold: float = 0.92) -> Optional[dict]:
        """
        Check if a similar query already has a cached solution.
        Returns the cached answer dict if score > threshold, else None.
        """
        collection = _solutions_collection(project_id, tenant_id)
        vector = _embed_query(query)
        if not vector:
            return None

        # Search for 1 nearest neighbor
        hits = _qdrant_search(collection, vector, top_k=1)
        if not hits:
            return None

        best = hits[0]
        score = best.get("score", 0.0)
        if score > threshold:
            payload = best.get("payload", {})
            print(f"[context] semantic cache hit: score={score:.4f}")
            return {
                "answer": payload.get("answer"),
                "score": score,
                "cached": True,
                "intent": payload.get("intent"),
                "sources": payload.get("sources", []),
            }
        return None

    def store_solution(
        self,
        query: str,
        answer: str,
        project_id: str,
        tenant_id: str,
        intent: str = "unknown",
        sources: list[str] = None,
        *,
        verified: bool = False,
        metadata: dict | None = None,
    ):
        """Save a (Query, Answer) pair to the semantic cache."""
        collection = _solutions_collection(project_id, tenant_id)
        vector = _embed_query(query)
        if not vector:
            return

        # Ensure collection exists (assume 768 dim for nomic-embed-text)
        _ensure_collection(collection, len(vector))

        # Deterministic ID based on query
        pt_id = str(hashlib.md5(f"{tenant_id}:{project_id}:{query}".encode()).hexdigest())

        point = {
            "id": pt_id,
            "vector": vector,
            "payload": {
                "query": query,
                "answer": answer,
                "intent": intent,
                "sources": sources or [],
                "verified": bool(verified),
                "metadata": {
                    "project_id": project_id,
                    "tenant_id": tenant_id,
                    "intent": intent,
                    "sources": sources or [],
                    "verified": bool(verified),
                    **(metadata or {}),
                },
                "timestamp": _now_iso() if "_now_iso" in globals() else "",
            }
        }

        _, error = _shared_upsert_point(collection, vector, point["payload"], point_id=pt_id, timeout=10)
        if error:
            print(f"[context] cache store error: {error}")

    def retrieve_empty(self) -> dict:
        """Return empty context structure (when vector store has no data yet)."""
        return {"chunks": [], "graph": [], "context": "", "sources": []}


# ──────────────────────────────────────────────
# ASYNC INTELLIGENCE LAYER
# ──────────────────────────────────────────────

async def _embed_query_async(text: str) -> list[float]:
    """Non-blocking embed: runs sync HTTP call in a thread."""
    return await asyncio.to_thread(_embed_query, text)


async def _qdrant_search_async(collection: str, vector: list[float],
                               top_k: int,
                               filters: Optional[dict] = None) -> list[dict]:
    """Non-blocking Qdrant search."""
    return await asyncio.to_thread(_qdrant_search, collection, vector, top_k, filters)


async def _neo4j_centrality_expand(file_paths: list[str], project_id: str,
                                   tenant_id: str) -> list[dict]:
    """
    Async Neo4j expansion that also returns a centrality proxy (degree count).
    Returns list of {type, name, file, line, relation, centrality}.
    """
    if not file_paths:
        return []

    def _query() -> list[dict]:
        try:
            from neo4j import GraphDatabase
        except ImportError:
            return []

        nodes: list[dict] = []
        try:
            driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASS))
            with driver.session() as session:
                result = session.run("""
                    MATCH (f:File)-[:DEFINES]->(s)
                    WHERE f.path IN $paths
                      AND f.project_id = $pid
                      AND f.tenant_id  = $tid
                    OPTIONAL MATCH (s)-[r]-()
                    WITH labels(s)[0] AS type, s.name AS name,
                         f.path AS file, s.line AS line,
                         count(r) AS degree
                    RETURN type, name, file, line, degree
                    LIMIT 150
                """, paths=file_paths, pid=project_id, tid=tenant_id)
                max_deg = 1
                rows = []
                for rec in result:
                    deg = rec["degree"] or 0
                    if deg > max_deg:
                        max_deg = deg
                    rows.append({
                        "type":       rec["type"] or "Symbol",
                        "name":       rec["name"],
                        "file":       rec["file"],
                        "line":       rec["line"] or 0,
                        "relation":   "defined_in",
                        "_degree":    deg,
                    })
                for row in rows:
                    row["centrality"] = round(row.pop("_degree") / max_deg, 4)
                    nodes.append(row)
            driver.close()
        except Exception as exc:
            print(f"[context] Neo4j centrality error: {exc}")
        return nodes

    return await asyncio.to_thread(_query)


def _recency_score(timestamp_iso: Optional[str]) -> float:
    """Convert an ISO timestamp to a 0–1 recency score (1 = now, 0 = >90 days old)."""
    if not timestamp_iso:
        return 0.0
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age_days = (datetime.now(tz=timezone.utc) - ts).total_seconds() / 86400
        return max(0.0, 1.0 - age_days / 90.0)
    except Exception:
        return 0.0


async def graph_aware_retrieve(
    query: str,
    project_id: str,
    tenant_id: str,
    top_k: int = DEFAULT_TOP_K,
) -> dict:
    """
    Hybrid retrieval combining Qdrant semantic search with Neo4j structural
    expansion and recency scoring.

    Scoring weights:
        0.45 × semantic score  (cosine similarity from Qdrant)
        0.35 × centrality      (normalised node degree in knowledge graph)
        0.20 × recency         (1 = now, 0 = >90 days old)

    Returns:
        {
            "chunks":  list of scored chunks sorted by hybrid_score DESC,
            "graph":   list of graph nodes with centrality,
            "context": formatted string ready for LLM prompt injection,
            "sources": deduplicated list of source file paths,
        }
    """
    collection = _qdrant_collection(project_id, tenant_id)

    # 1. Embed + search run concurrently — the embed must finish before search,
    #    but Neo4j expansion can start as soon as we have file hits.
    vector = await _embed_query_async(query)
    if not vector:
        return {"chunks": [], "graph": [], "context": "", "sources": []}

    # Search with 2× top_k as seed pool so Neo4j expansion has more candidates
    raw_hits = await _qdrant_search_async(collection, vector, top_k * 2)

    # Build chunk list + collect unique files for graph expansion
    file_hits: set[str] = set()
    seed_chunks: list[dict] = []
    for hit in raw_hits:
        pl = hit.get("payload", {})
        chunk = {
            "file":           pl.get("file", ""),
            "chunk":          pl.get("chunk", 0),
            "text":           pl.get("text", ""),
            "semantic_score": round(hit.get("score", 0.0), 4),
            "line":           pl.get("line", 0),
            "timestamp":      pl.get("timestamp"),
        }
        seed_chunks.append(chunk)
        if chunk["file"]:
            file_hits.add(chunk["file"])

    # 2. Neo4j centrality expansion (parallel with nothing — already have files)
    try:
        graph_nodes = await _neo4j_centrality_expand(
            list(file_hits), project_id, tenant_id
        )
    except Exception as exc:
        print(f"[context] Neo4j centrality error: {exc}")
        graph_nodes = []

    # Map file paths to their maximum centrality found in symbols
    file_centrality = {}
    for node in graph_nodes:
        f = node.get("file")
        if f:
            file_centrality[f] = max(file_centrality.get(f, 0.0), node.get("centrality", 0.0))

    # 3. Hybrid scoring for chunks
    # Scoring: 0.45 semantic + 0.35 centrality + 0.20 recency
    scored_chunks = []
    for chunk in seed_chunks:
        semantic   = chunk["semantic_score"]
        recency    = _recency_score(chunk.get("timestamp"))
        centrality = file_centrality.get(chunk["file"], 0.0)
        
        hybrid = round(
            0.45 * semantic + 
            0.35 * centrality + 
            0.20 * recency, 
            4
        )
        scored_chunks.append({**chunk, "hybrid_score": hybrid, "centrality": centrality})

    scored_chunks.sort(key=lambda c: -c["hybrid_score"])

    # 4. Hybrid scoring for graph nodes (L3 specialized bits)
    scored_graph = []
    for node in graph_nodes:
        centrality = node.get("centrality", 0.0)
        # Note: Nodes don't have a semantic similarity to query directly in this view,
        # but their centrality itself acts as the structural signal (0.35 weight in prompt).
        # We'll normalize their structural score.
        hybrid = round(0.35 * centrality, 4)
        scored_graph.append({**node, "hybrid_score": hybrid})
    scored_graph.sort(key=lambda n: -n["hybrid_score"])

    top_chunks = scored_chunks[:top_k]
    context    = await _format_context_cognitive(
        top_chunks, scored_graph, project_id, tenant_id
    )

    return {
        "chunks":  top_chunks,
        "graph":   scored_graph,
        "context": context,
        "sources": sorted(file_hits),
    }


async def build_proactive_context(
    description: Optional[str] = None,
    agent_name: Optional[str] = None,
    project_id: str = "",
    tenant_id: str = "",
    task: Optional[dict] = None,
) -> dict:
    """
    Module 2.2 — Proactive Memory Injection: 
    Gather 4 types of intelligence in parallel before execution.
    """
    task = task or {}
    if description is None:
        description = str(task.get("description") or "")
    if agent_name is None:
        agent_name = str(
            task.get("agent_name")
            or task.get("assigned_agent")
            or task.get("preferred_agent")
            or "orchestrator"
        )

    empty = {
        "proven_approaches": [],
        "watch_out_for": [],
        "relevant_code": [],
        "this_agent_habits": [],
        "solutions": [],
        "errors": [],
        "code_context": [],
        "agent_hints": [],
    }

    # 1. Embed current task description
    task_embedding = await _embed_query_async(description)
    if not task_embedding:
        return empty

    # 2. Targeted searches in specialized collections
    s_coll = f"{tenant_id}_{project_id}_solutions"
    e_coll = f"{tenant_id}_{project_id}_errors"
    c_coll = f"{tenant_id}_{project_id}_code"
    a_coll = f"{tenant_id}_{project_id}_agent_behaviors"

    # Ensure collections exist (assume 768 dim for nomic-embed-text)
    dim = len(task_embedding)
    _ensure_collection(s_coll, dim)
    _ensure_collection(e_coll, dim)
    _ensure_collection(c_coll, dim)
    _ensure_collection(a_coll, dim)

    # Define parallel search tasks with filtering for high confidence
    tasks = [
        # Similar Solutions (Success > 70%)
        _qdrant_search_async(s_coll, task_embedding, 3),
        # Likely Errors
        _qdrant_search_async(e_coll, task_embedding, 3),
        # Relevant code snippets (Seed)
        _qdrant_search_async(c_coll, task_embedding, 5),
        # Agent Habits for this task type
        _qdrant_search_async(a_coll, task_embedding, 2, 
                             filters={"must": [{"key": "agent_name", "match": {"value": agent_name}}]})
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    def _safe_payloads(res):
        if isinstance(res, list):
            return [hit.get("payload", {}) for hit in res if hit.get("score", 0) > 0.7]
        return []

    solutions = _safe_payloads(results[0])
    errors = _safe_payloads(results[1])
    code_context = _safe_payloads(results[2])
    agent_hints = _safe_payloads(results[3])

    return {
        "proven_approaches": solutions,
        "watch_out_for": errors,
        "relevant_code": code_context,
        "this_agent_habits": agent_hints,
        "solutions": solutions,
        "errors": errors,
        "code_context": code_context,
        "agent_hints": agent_hints,
    }


async def estimate_task_eta(task_id: str, tenant_id: str) -> dict:
    """
    Predict the remaining time for a task based on its type and current heartbeat history.
    Returns {predicted_total_ms, elapsed_ms, remaining_ms, confidence}.
    """
    from services.streaming.core.db import async_db
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                # 1. Get task info and historical p90 for this type
                from services.streaming.core.schema_compat import get_task_pk_column
                task_pk = await get_task_pk_column(cur)
                await cur.execute("""
                    SELECT t.task_type, t.assigned_agent, 
                           EXTRACT(EPOCH FROM (NOW() - t.started_at)) * 1000 as elapsed,
                           p.p90_duration_ms
                    FROM tasks t
                    LEFT JOIN task_success_prediction p 
                      ON p.tenant_id = t.tenant_id AND p.task_type = t.task_type AND p.agent_name = t.assigned_agent
                    WHERE t.{task_pk} = %s
                """.format(task_pk=task_pk), (task_id,))
                row = await cur.fetchone()
                if not row or not row.get("task_type"):
                    return {"error": "task_not_found"}

                task_type = row["task_type"]
                elapsed = row["elapsed"] or 0
                expected_p90 = row["p90_duration_ms"] or 60000 # Fallback 1m

                # 2. Analyze heartbeat frequency for "liveness"
                await cur.execute("""
                    SELECT COUNT(*) as beats, 
                           AVG(EXTRACT(EPOCH FROM (beat_at - lag_beat)) * 1000) as avg_gap
                    FROM (
                        SELECT beat_at, LAG(beat_at) OVER (ORDER BY beat_at) as lag_beat
                        FROM heartbeats WHERE task_id = %s
                    ) sub
                """, (task_id,))
                h_row = await cur.fetchone()
                
                beats = h_row.get("beats", 0)
                avg_gap = h_row.get("avg_gap", 5000) # Fallback 5s

                # 3. Intelligent Regression + p90 ceiling
                # Compare current velocity (avg_gap) with the "standard" flow.
                # If avg_gap is significantly lower than average, agent is accelerating.
                
                # Assume standard gap is 5s (5000ms)
                standard_gap = 5000
                velocity_multiplier = max(0.5, min(2.0, avg_gap / standard_gap))
                
                # If elapsed > expected_p90, we are in "overtime" — increase predicted exponentially
                if elapsed > expected_p90:
                    overtime_factor = (elapsed / expected_p90) ** 1.1 # Slow linear growth
                    predicted = elapsed + (avg_gap * 3 * overtime_factor)
                    confidence = 0.3 # Low confidence in overtime
                else:
                    # Normal flow: predicted is based on historical p90 weighted by current velocity
                    # If we have no beats, we fall back to p90.
                    if beats > 0:
                        predicted = max(expected_p90 * velocity_multiplier, elapsed + (avg_gap * 2))
                        confidence = min(0.95, 0.4 + (beats * 0.05))
                    else:
                        predicted = expected_p90
                        confidence = 0.4

                return {
                    "task_id": task_id,
                    "task_type": task_type,
                    "elapsed_ms": round(elapsed, 2),
                    "predicted_total_ms": round(predicted, 2),
                    "remaining_ms": round(max(0, predicted - elapsed), 2),
                    "confidence": round(confidence, 2)
                }
    except Exception as e:
        return {"error": str(e)}


async def get_success_prediction(category: str, agent_name: str, tenant_id: str) -> dict:
    """
    Module 5.1: Reward Prediction.
    Queries the materialized view to get historical success rates.
    """
    from services.streaming.core.db import async_db
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT success_rate, avg_duration_ms, p90_duration_ms, sample_count
                    FROM task_success_prediction
                    WHERE tenant_id = %s AND task_type = %s AND agent_name = %s
                """, (tenant_id, category, agent_name))
                row = await cur.fetchone()
                if row:
                    return dict(row)
                return {"success_rate": 0.5, "sample_count": 0, "status": "insufficient_data"}
    except Exception:
        return {"success_rate": 0.5, "sample_count": 0, "status": "query_error"}


async def collect_proactive_signals(
    description: str,
    project_id: str,
    tenant_id: str,
    agent_name: str = "orchestrator",
    category: str = "generic",
    file_path: Optional[str] = None
) -> dict:
    """
    Module 6: Unified Pre-Execution Orchestrator.
    Parallelizes all intelligence signals to inform the agent before execution.
    """
    # Parallel gather of all potency signals
    tasks = [
        build_proactive_context(description, agent_name, project_id, tenant_id),
        find_similar_past_solutions(description, project_id, tenant_id),
        get_success_prediction(category, agent_name, tenant_id)
    ]
    if file_path:
        tasks.append(assess_change_impact(file_path, project_id, tenant_id))
    
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    # Process results safely
    proactive = results[0] if not isinstance(results[0], Exception) else {}
    past_sol = results[1] if not isinstance(results[1], Exception) else []
    prediction = results[2] if not isinstance(results[2], Exception) else {}
    impact = results[3] if len(results) > 3 and not isinstance(results[3], Exception) else {}

    # Build an enriched hint for the solver
    hint = "\n=== UNIFIED PRE-EXECUTION INTELLIGENCE ===\n"
    if impact and impact.get("risk_level") == "high":
        hint += f"⚠️ HIGH RISK: Blast radius is {impact['blast_radius']} files. Usually changes with: {impact.get('usually_changed_together')}\n"
    
    if prediction.get("success_rate", 0) < 0.4 and prediction.get("sample_count", 0) > 2:
        hint += f"💡 SYSTEM ADVISORY: Historical success rate for this pattern is low ({int(prediction['success_rate']*100)}%). Consider decomposing this task.\n"

    if proactive.get("proven_approaches"):
        hint += "\nPROVEN APPROACHES:\n"
        for sol in proactive["proven_approaches"][:2]:
            hint += f"- {sol.get('solution_summary', sol.get('answer', ''))[:200]}...\n"

    return {
        "hint": hint,
        "proactive_context": proactive,
        "impact_report": impact,
        "success_prediction": prediction,
        "similar_past_solutions": past_sol
    }


async def assess_change_impact(file_path: str, project_id: str, tenant_id: str) -> dict:
    """
    Calculate the potential blast radius of changing a file.
    Queries Neo4j for reverse dependencies and historical co-changes.
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return {"error": "neo4j_driver_missing", "risk": "unknown"}

    query = """
    MATCH (f:File {path: $path, project_id: $pid, tenant_id: $tid})
    OPTIONAL MATCH (affected:File)-[:DEPENDS_ON*1..3]->(f)
    OPTIONAL MATCH (f)-[rel:CHANGES_WITH]-(hot_neighbor:File)
    WHERE rel.co_change_count >= 3
    RETURN 
        count(DISTINCT affected) as blast_radius,
        collect(DISTINCT affected.path)[..10] as affected_files,
        collect(DISTINCT hot_neighbor.path)[..5] as usually_changed_together,
        f.change_frequency as this_file_volatility
    """
    
    def _query():
        auth = (NEO4J_USER, NEO4J_PASS)
        with GraphDatabase.driver(NEO4J_URI, auth=auth) as driver:
            with driver.session() as session:
                res = session.run(query, path=file_path, pid=project_id, tid=tenant_id)
                rec = res.single()
                if not rec:
                    return None
                return {
                    "blast_radius": rec["blast_radius"],
                    "affected_files": rec["affected_files"],
                    "usually_changed_together": rec["usually_changed_together"],
                    "volatility": rec["this_file_volatility"] or 1,
                    "risk_level": "high" if rec["blast_radius"] > 10 else "medium" if rec["blast_radius"] > 0 else "low"
                }

    try:
        result = await asyncio.to_thread(_query)
        return result or {"blast_radius": 0, "risk_level": "low", "note": "file_not_found_in_graph"}
    except Exception as exc:
        print(f"[context] impact assessment error: {exc}")
        return {"error": str(exc), "risk": "unknown"}


async def find_similar_past_solutions(task_description: str, project_id: str, tenant_id: str) -> list[dict]:
    """
    Search Neo4j for tasks with similar structural context that were successfully resolved.
    """
    try:
        from neo4j import GraphDatabase
    except ImportError:
        return []

    # Query logic: Find tasks related via SIMILAR_TO or sharing common file touches/category
    query = """
    MATCH (s:Solution)-[:RESOLVED_BY]-(past_t:Task)
    WHERE past_t.project_id = $pid AND past_t.tenant_id = $tid
      AND past_t.status = 'done'
      AND (past_t.description CONTAINS $desc_snippet OR past_t.category = $category)
    MATCH (past_a:Agent)-[:SUCCEEDED_ON]->(past_t)
    OPTIONAL MATCH (past_t)-[:CAUSED_ERROR]->(e:Error)
    RETURN 
        past_t.id as task_id,
        past_t.description as description,
        s.description as solution,
        past_a.name as best_agent,
        collect(DISTINCT e.type) as known_errors,
        s.resolved_at_layer as layer
    ORDER BY past_t.created_at DESC
    LIMIT 5
    """
    
    # We use a broad snippet of the current task description to find structural matches
    desc_snippet = task_description[:25] if len(task_description) > 25 else task_description
    category = "generic" # We'd ideally pass this in

    def _query():
        auth = (NEO4J_USER, NEO4J_PASS)
        with GraphDatabase.driver(NEO4J_URI, auth=auth) as driver:
            with driver.session() as session:
                res = session.run(query, pid=project_id, tid=tenant_id, 
                                 desc_snippet=desc_snippet, category=category)
                return [dict(r) for r in res]

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        print(f"[context] find similar past solutions error: {exc}")
        return []


async def cluster_recent_failures(project_id: str, tenant_id: str, days: int = 7) -> list[dict]:
    """
    Groups recent failures by semantic similarity to identify systemic patterns.
    Uses K-Means clustering on Qdrant vectors.
    """
    try:
        import numpy as np
        from sklearn.cluster import KMeans
    except ImportError:
        print("[context] clustering requires numpy and scikit-learn")
        return []

    collection = f"{tenant_id}_{project_id}_errors"
    
    # 1. Fetch recent errors from Qdrant
    # In a real scenario, we'd use scroll with filters. For now, we search with a generic query if collection empty.
    # But here we assume the collector has been populating it.
    points, error = _shared_scroll_points(collection, limit=100, with_payload=True, with_vector=True, timeout=15)
    if error:
        print(f"[context] clustering fetch error: {error}")
        return []

    if len(points) < 3:
        return []

    # 2. Extract vectors and cluster
    vectors = np.array([p["vector"] for p in points if p.get("vector")])
    if len(vectors) < 3: return []
    
    n_clusters = min(5, len(vectors) // 2)
    kmeans = KMeans(n_clusters=n_clusters, random_state=42, n_init=10)
    labels = kmeans.fit_predict(vectors)

    clusters = []
    for i in range(n_clusters):
        cluster_points = [points[j] for j, l in enumerate(labels) if l == i]
        if not cluster_points: continue
        
        # Module 2.3: Dynamic Pattern Recognition via LLM
        # We take the unique error types and a few context snippets
        error_types = list(set(p["payload"].get("error_type", "Unknown") for p in cluster_points))
        contexts = [p["payload"].get("context", "")[:150] for p in cluster_points[:3]]
        
        # For Maximum Potency, we'd call a small LLM (e.g. Ollama/Haiku) to name this pattern
        # Since we are in the context_retriever, we'll use a refined heuristic or an internal call
        pattern_name = f"Systemic {error_types[0]}" if error_types else "Unknown Pattern"
        if len(error_types) > 1:
            pattern_name += f" (+{len(error_types)-1} variants)"
        
        clusters.append({
            "pattern": pattern_name,
            "count": len(cluster_points),
            "severity": "high" if len(cluster_points) > 10 else "medium",
            "examples": contexts,
            "impacted_agents": list(set(p["payload"].get("agent_name", "unknown") for p in cluster_points))
        })

    return sorted(clusters, key=lambda x: x["count"], reverse=True)


async def compute_semantic_agent_score(query: str, agent_name: str, project_id: str, tenant_id: str) -> float:
    """
    Calculates how well an agent's past successful solutions match the current query.
    Returns a score between 0.0 and 1.0.
    """
    vector = await _embed_query_async(query)
    if not vector: return 0.5
    
    collection = f"{tenant_id}_{project_id}_solutions"
    filters = {"must": [{"key": "agent_name", "match": {"value": agent_name}}]}
    
    hits = await _qdrant_search_async(collection, vector, top_k=5, filters=filters)
    if not hits:
        return 0.5 # Neutral score for new agents or agents with no history
    
    avg_score = sum(h.get("score", 0.0) for h in hits) / len(hits)
    return round(avg_score, 4)


# ──────────────────────────────────────────────
# MODULE-LEVEL CONVENIENCE
# ──────────────────────────────────────────────

_retriever = ContextRetriever()


def retrieve_context(query: str, project_id: str, tenant_id: str,
                     top_k: int = DEFAULT_TOP_K,
                     project_path: Optional[str] = None) -> dict:
    """Sync convenience wrapper (use graph_aware_retrieve for async callers)."""
    r = ContextRetriever(project_path=project_path, top_k=top_k)
    return r.retrieve(query, project_id=project_id, tenant_id=tenant_id)


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────

if __name__ == "__main__":
    import sys

    query      = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else "authentication"
    project_id = env_get("PROJECT_ID", default="sinc")
    tenant_id  = env_get("TENANT_ID", default="local")

    print(f"Query: {query!r}  project={project_id} tenant={tenant_id}")
    result = retrieve_context(query, project_id=project_id, tenant_id=tenant_id)
    print(f"\n=== {len(result['chunks'])} chunks, {len(result['graph'])} graph nodes ===\n")
    print(result["context"] or "(no context — run ingest first)")
