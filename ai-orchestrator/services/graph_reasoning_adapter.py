from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Optional

import services.context_retriever as context_retriever

log = logging.getLogger("orch.graph_reasoning")


@dataclass
class UnifiedGraphReasoningResult:
    structural_context: str
    graph_result: dict[str, Any]
    got_result: Any = None
    solution: Optional[str] = None
    steps: list[str] | None = None
    cache_level: Optional[str] = None
    tokens_saved: int = 0
    llm_needed: bool = True
    confidence: float = 0.0


def _lookup_got_sync(orch: Any, description: str, task_type: str):
    got = getattr(orch, "_got", None)
    if not got:
        return None
    embedder_func = None
    memory = getattr(orch, "_memory", None)
    l2 = getattr(memory, "l2", None) if memory else None
    if l2 and getattr(l2, "embedder_func", None):
        embedder_func = l2.embedder_func
    return got.find_or_create_reasoning(description, task_type, embedder_func)


async def resolve_graph_reasoning(
    description: str,
    task_type: str,
    project_id: str,
    tenant_id: str,
    orch: Any | None = None,
) -> UnifiedGraphReasoningResult:
    """
    Canonical graph-reasoning path for the cognitive runtime.

    Structural GraphRAG and GoT are still both used, but only through this adapter.
    This removes ad hoc branching in callers and keeps one contract for:
      - graph context enrichment
      - reusable reasoning lookup
      - confidence/caching metadata
    """
    if orch is None:
        from services.cognitive_orchestrator import get_orchestrator

        orch = get_orchestrator()

    graph_task = context_retriever.graph_aware_retrieve(
        description,
        project_id,
        tenant_id,
    )
    got_task = asyncio.to_thread(_lookup_got_sync, orch, description, task_type)

    graph_result, got_result = await asyncio.gather(graph_task, got_task, return_exceptions=True)

    if isinstance(graph_result, Exception):
        log.warning("graph_adapter_structural_error error=%s", graph_result)
        graph_result = {"chunks": [], "graph": [], "context": "", "sources": []}

    if isinstance(got_result, Exception):
        log.warning("graph_adapter_got_error error=%s", got_result)
        got_result = None

    structural_context = str((graph_result or {}).get("context") or "").strip()
    structural_hint = ""
    if structural_context:
        structural_hint = f"\nStructural Analysis (Canonical Graph Path): {structural_context}"

    if got_result and getattr(got_result, "solution", None):
        return UnifiedGraphReasoningResult(
            structural_context=structural_context,
            graph_result=graph_result or {},
            got_result=got_result,
            solution=str(got_result.solution),
            steps=list(getattr(got_result, "steps", []) or []),
            cache_level=str(getattr(got_result, "source", "neo4j_existing")),
            tokens_saved=2000,
            llm_needed=False,
            confidence=max(float(getattr(got_result, "confidence", 0.0) or 0.0), 0.85),
        )

    return UnifiedGraphReasoningResult(
        structural_context=structural_context,
        graph_result=graph_result or {},
        got_result=None,
        solution=None,
        steps=[],
        cache_level=None,
        tokens_saved=0,
        llm_needed=True,
        confidence=0.5 if structural_context else 0.0,
    )
