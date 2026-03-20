"""
cognitive_orchestrator.py
=========================
Elite V2 Cognitive Runtime Orchestrator — Pillar III maturity.

Features:
- Global Context (ContextVar) for trace/tenant propagation.
- Strict Pydantic models for type safety.
- Component Registry for resource lifecycle and health.
- Persistent HTTP connection pooling for LLM providers.
- Hardened: Zero-Lock Migrations + Task Registry shutdown (Level 5+)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, Field, field_validator
from services.admission_control import AdmissionController, Decision
from services.http_client import create_resilient_client
from services.streaming.core.config import env_get
from services.streaming.core.lifecycle import get_task_registry

log = logging.getLogger("orch.cognitive")

# ── Context Management ────────────────────────────────────────────────────────

@dataclass
class OrchestratorContext:
    tenant_id: str = ""
    trace_id: str = "none"
    project_id: str = ""
    start_time: float = field(default_factory=time.perf_counter)
    metadata: Dict[str, Any] = field(default_factory=dict)

# Global context variable for automatic tenant/trace propagation
_context: ContextVar[OrchestratorContext] = ContextVar(
    "orchestrator_context",
    default=OrchestratorContext()
)

def get_context() -> OrchestratorContext:
    return _context.get()

def set_context(tenant_id: str, trace_id: str = "none", project_id: str = "") -> OrchestratorContext:
    if not tenant_id or tenant_id == "local":
        raise MissingTenantError("Explicit tenant_id is required for Pillar III operations.")
    ctx = OrchestratorContext(tenant_id=tenant_id, trace_id=trace_id, project_id=project_id)
    _context.set(ctx)
    return ctx

# ── Models ────────────────────────────────────────────────────────────────────

class MissingTenantError(ValueError):
    """Raised when a tenant_id is missing or null in the cognitive pipeline."""
    pass

class CognitiveTask(BaseModel):
    id: str = Field(default_factory=lambda: f"task-{int(time.time())}")
    title: str = ""
    description: str
    task_type: str = "generic"
    project_id: Optional[str] = ""
    tenant_id: str
    metadata: Dict[str, Any] = {}

    @field_validator("description")
    @classmethod
    def description_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("description must not be empty")
        return v

class TaskResult(BaseModel):
    task_id:      str
    solution:     str
    steps:        List[str] = []
    planner:      str           # "deterministic" | "mcts" | "llm"
    cache_level:  str           # "L0_rules" | "L1_redis" ... | "llm"
    llm_used:     bool
    tokens_saved: int           # tokens NOT sent to LLM because of cache hit
    tokens_used:  int           # tokens actually consumed (0 on cache hit)
    latency_ms:   float
    error:        Optional[str] = None
    metadata:     Dict[str, Any] = {}


@dataclass
class CognitiveRuntimeConfig:
    confidence_threshold: float = field(default_factory=lambda: float(env_get("ORCHESTRATOR_CONFIDENCE_THRESHOLD", default="0.7")))
    system_mode: str = field(default_factory=lambda: str(env_get("ORCHESTRATOR_SYSTEM_MODE", default="normal")).lower())
    bypass_admission: bool = field(default_factory=lambda: env_get("ORCHESTRATOR_BYPASS_ADMISSION", default="0") == "1")

# ── Component Registry ───────────────────────────────────────────────────────

@runtime_checkable
class CognitiveComponent(Protocol):
    """Protocol for all cognitive components (L0-L4, Planner)."""
    async def get_status(self) -> Dict[str, Any]: ...

class ComponentRegistry:
    """Manages the lifecycle and health of all orchestrator sub-components."""
    def __init__(self):
        self._components: Dict[str, Any] = {}
        self._initialized = False

    def register(self, name: str, component: Any):
        self._components[name] = component
        log.info("component_registered name=%s", name)

    def get(self, name: str) -> Any:
        return self._components.get(name)

    async def check_health(self) -> Dict[str, Any]:
        report = {}
        for name, comp in self._components.items():
            try:
                if hasattr(comp, "get_status"):
                    report[name] = await comp.get_status()
                else:
                    report[name] = {"status": "available"}
            except Exception as e:
                report[name] = {"status": "error", "message": str(e)}
        return report

# ── Metrics & Lifecycle ──────────────────────────────────────────────────────

class _Metrics:
    def __init__(self, window: int = 1000):
        self._latencies: deque[float] = deque(maxlen=window)
        self._bypassed:  deque[bool]  = deque(maxlen=window)
        self._tokens_saved: int = 0
        self._tokens_used:  int = 0

    def record(self, latency_ms: float, llm_used: bool, tokens_saved: int, tokens_used: int):
        self._latencies.append(latency_ms)
        self._bypassed.append(not llm_used)
        self._tokens_saved += tokens_saved
        self._tokens_used  += tokens_used

    def snapshot(self) -> Dict[str, Any]:
        n = len(self._latencies)
        if n == 0: return {"requests": 0}
        avg_lat = sum(self._latencies) / n
        bypass_rate = sum(self._bypassed) / n
        return {
            "requests": n,
            "bypass_rate": round(bypass_rate, 4),
            "avg_latency_ms": round(avg_lat, 2),
            "tokens_saved": self._tokens_saved,
            "tokens_used": self._tokens_used
        }

class CognitiveOrchestrator:
    """
    Elite V2 Cognitive Orchestrator — Hardened Level 5+.
    Central cognitive engine with Zero-Lock Migrations and Task Registry.
    """
    def __init__(self):
        self.registry = ComponentRegistry()
        self.metrics = _Metrics()
        self.config = CognitiveRuntimeConfig()
        self._http_client: Optional[httpx.AsyncClient] = None
        self._initialized = False
        self._lock = asyncio.Lock()
        # Compatibility aliases for legacy callers/tests
        self._planner = None
        self._memory = None
        self._admission = None
        self._graph_intel = None
        self._got = None
        self._rules = None

    async def _ensure_init(self):
        await self.initialize()
        return self

    async def initialize(self) -> httpx.AsyncClient:
        """Professional initialization with locking, pooling, and hardened migrations."""
        async with self._lock:
            if self._initialized and self._http_client:
                return self._http_client

            if not self._http_client:
                self._http_client = create_resilient_client()

            # 0. Hardened Migrations: Zero-Lock schema bootstrap (Level 5+)
            try:
                from services.streaming.core.migrations import ensure_pillar_iii_schemas
                await ensure_pillar_iii_schemas()
            except Exception as e:
                log.error("init_failure component=migrations error=%s", e)

            # 1. Load Core Planner
            try:
                from services.mcts_planner import get_planner
                self._planner = get_planner()
                self.registry.register("planner", self._planner)
            except Exception as e:
                log.warning("init_failure component=planner error=%s", e)

            # 2. Load Memory Layers (L0-L4)
            await self._init_memory_layers()

            # 3. Admission Control
            try:
                from services.admission_control import AdmissionController
                self._admission = AdmissionController()
                self.registry.register("admission", self._admission)
            except Exception as e:
                log.warning("init_failure component=admission error=%s", e)

            # 4. Graph Intelligence
            try:
                from services.graph_intelligence import get_graph_intelligence
                self._graph_intel = get_graph_intelligence()
                self.registry.register("graph_intel", self._graph_intel)
            except Exception as e:
                log.warning("init_failure component=graph_intel error=%s", e)

            try:
                from services.graph_of_thought import GraphOfThought
                neo4j_uri = env_get("NEO4J_URI", default="")
                neo4j_user = env_get("NEO4J_USER", default="neo4j")
                neo4j_password = env_get(
                    "NEO4J_PASS",
                    default=env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/")[-1],
                )
                if neo4j_uri:
                    self._got = GraphOfThought(neo4j_uri, neo4j_user, neo4j_password)
            except Exception as e:
                log.warning("init_failure component=got error=%s", e)

            self._initialized = True
            log.info("orchestrator_initialization_complete pool_size=100")
            return self._http_client

    async def _init_memory_layers(self):
        try:
            from services.memory_layers import (
                MemoryHierarchyRouter, L0RuleEngine, L1DeterministicCache,
                L2SemanticMemory, L3GraphReasoning, L4EventMemory
            )
            l0 = L0RuleEngine()
            l1 = L1DeterministicCache(redis_url=env_get("REDIS_URL", default="redis://localhost:6379"))

            # audit C2: L2 was constructed without embedder_func, causing silent TypeError.
            # Wire semantic_backend.embed_text so L2 actually performs vector search.
            l2 = None
            try:
                from services.semantic_backend import embed_text as _raw_embed

                def _embedder(text: str) -> list:
                    vec, err = _raw_embed(text)
                    if err:
                        log.warning("l2_embedder_error error=%s", err)
                    return vec or []

                l2 = L2SemanticMemory(
                    qdrant_host=env_get("QDRANT_HOST", default="localhost"),
                    qdrant_port=int(env_get("QDRANT_PORT", default=6333)),
                    embedder_func=_embedder,
                )
            except Exception as exc:
                log.warning("init_failure component=l2_semantic error=%s", exc)

            router = MemoryHierarchyRouter(l0=l0, l1=l1, l2=l2)
            self._memory = router
            self._rules = l0
            self.registry.register("memory", router)
        except Exception as e:
            log.warning("init_failure component=memory_layers error=%s", e)

    async def process(self, task_input: Union[Dict, CognitiveTask]) -> TaskResult:
        """Process a single task through the full cognitive pipeline."""
        client = await self.initialize()

        task = task_input if isinstance(task_input, CognitiveTask) else CognitiveTask(**task_input)
        ctx = set_context(tenant_id=task.tenant_id, trace_id=task.id, project_id=task.project_id or "")

        log.info("task_start id=%s type=%s tenant=%s", task.id, task.task_type, task.tenant_id)

        adm_ctrl = self.registry.get("admission")
        if adm_ctrl:
            admission = await adm_ctrl.evaluate_batch([task.model_dump()], task.tenant_id)
            decision = admission.get(task.id)
            if decision and decision.decision != Decision.ADMIT:
                log.warning("task_admission_rejected id=%s reason=%s", task.id, decision.reason)
                return self._error_res(task, f"ADMISSION_FAILED: {decision.reason}")

        try:
            from services.cognitive_graph import get_cognitive_graph
            graph = get_cognitive_graph()
            if not graph:
                raise RuntimeError("CognitiveGraph unavailable")

            initial_state = {
                "task": task.model_dump(),
                "description": task.description,
                "task_type": task.task_type,
                "tenant_id": task.tenant_id,
                "start_time": ctx.start_time,
                "http_client": client
            }

            final_state = await graph.ainvoke(initial_state)

            latency = (time.perf_counter() - ctx.start_time) * 1000
            result = TaskResult(
                task_id=task.id,
                solution=final_state.get("solution", "No solution generated"),
                steps=final_state.get("steps", []),
                planner=final_state.get("planner_name", "unknown"),
                cache_level=final_state.get("cache_level", "none"),
                llm_used=final_state.get("llm_used", False),
                tokens_saved=final_state.get("tokens_saved", 0),
                tokens_used=final_state.get("tokens_used", 0),
                latency_ms=round(latency, 2),
                metadata=final_state.get("metadata", {})
            )

            self.metrics.record(result.latency_ms, result.llm_used, result.tokens_saved, result.tokens_used)
            log.info("task_complete id=%s latency=%.2fms bypass=%s", task.id, latency, not result.llm_used)
            return result

        except Exception as e:
            log.error("task_failed id=%s error=%s", task.id, e, exc_info=True)
            return self._error_res(task, str(e))

    async def process_batch(self, tasks_input: List[Union[Dict, CognitiveTask]]) -> List[TaskResult]:
        """Process multiple tasks with semantic grouping and admission control."""
        if not tasks_input: return []

        tasks = [t if isinstance(t, CognitiveTask) else CognitiveTask(**t) for t in tasks_input]
        tenant_id = tasks[0].tenant_id

        client = await self.initialize()

        adm_ctrl = self.registry.get("admission")
        admitted = []
        results_map: Dict[str, TaskResult] = {}

        if adm_ctrl:
            decisions = await adm_ctrl.evaluate_batch([t.model_dump() for t in tasks], tenant_id)
            for t in tasks:
                d = decisions.get(t.id)
                if d and d.decision == Decision.ADMIT:
                    admitted.append(t)
                else:
                    results_map[t.id] = self._error_res(t, f"ADMISSION_FAILED: {d.reason if d else 'Unknown'}")
        else:
            admitted = tasks

        if not admitted:
            return [results_map[t.id] for t in tasks]

        groups: Dict[str, List[CognitiveTask]] = {}
        for t in admitted:
            groups.setdefault(t.task_type, []).append(t)

        all_tasks_to_run = []
        for ttype, tlist in groups.items():
            log.debug("batch_group_execution type=%s size=%d", ttype, len(tlist))
            all_tasks_to_run.extend(tlist)

        batch_results = await asyncio.gather(*[self.process(t) for t in all_tasks_to_run], return_exceptions=True)

        for i, res in enumerate(batch_results):
            tid = admitted[i].id
            if isinstance(res, Exception):
                results_map[tid] = self._error_res(admitted[i], f"BATCH_EXECUTION_FATAL: {res}")
            else:
                results_map[tid] = res

        return [results_map[t.id] for t in tasks]

    async def _process_legacy(self, task: Dict[str, Any], t0: float) -> TaskResult:
        log.warning(
            "cognitive_legacy_fallback task_id=%s task_type=%s",
            task.get("id"),
            task.get("task_type"),
        )
        task_model = task if isinstance(task, CognitiveTask) else CognitiveTask(**task)
        return self._error_res(task_model, "LEGACY_FALLBACK_UNAVAILABLE")

    def _error_res(self, task: CognitiveTask, error: str) -> TaskResult:
        return TaskResult(
            task_id=task.id, solution="", steps=[], planner="error", cache_level="none",
            llm_used=False, tokens_saved=0, tokens_used=0, latency_ms=0, error=error
        )

    def get_stats(self) -> Dict[str, Any]:
        return self.metrics.snapshot()

    async def shutdown(self):
        """Clean shutdown of pooled resources and all tracked background tasks."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

        # Hardened: Cancel all tracked asyncio tasks (No Zombie Tasks)
        registry = get_task_registry()
        await registry.shutdown(timeout=5.0)

        self._initialized = False
        log.info("orchestrator_shutdown_complete")

# ── Singleton & Convenience ──────────────────────────────────────────────────

_instance: Optional[CognitiveOrchestrator] = None
_LLM_RETRY_QUEUE_KEY = "sinc:llm_retry:{tenant_id}"
_LLM_RETRY_BASE_DELAY_S = 5
_LLM_RETRY_MAX_DELAY_S = 300
_LLM_RETRY_MAX_ATTEMPTS = 5

def get_orchestrator() -> CognitiveOrchestrator:
    global _instance
    if _instance is None:
        _instance = CognitiveOrchestrator()
    return _instance


def get_cognitive_graph():
    from services.cognitive_graph import get_cognitive_graph as _get_cognitive_graph
    return _get_cognitive_graph()


async def prepare_execution_context(task: Dict[str, Any], agent_name: str, tenant_id: str) -> Dict[str, Any]:
    """
    Compatibility + canonical helper used by task context routes and agent preflight.
    Composes proactive context, historical memory, impact, failure clustering,
    and graph reasoning into a single enriched prompt plus structured intelligence.
    """
    description = str(task.get("description") or task.get("title") or "").strip()
    task_type = str(task.get("task_type") or "generic").strip()
    project_id = str(task.get("project_id") or "").strip()
    files = list(task.get("files_affected") or task.get("files") or [])
    primary_file = str(task.get("primary_file") or (files[0] if files else "")).strip()

    from services.context_retriever import (
        assess_change_impact,
        build_proactive_context,
        cluster_recent_failures,
        find_similar_past_solutions,
    )
    from services.graph_reasoning_adapter import resolve_graph_reasoning

    impact_task = assess_change_impact(primary_file, project_id, tenant_id) if primary_file else asyncio.sleep(0, result={})
    proactive_task = build_proactive_context(description, agent_name, project_id, tenant_id)
    history_task = find_similar_past_solutions(description, project_id, tenant_id)
    failure_task = cluster_recent_failures(project_id, tenant_id)
    graph_task = resolve_graph_reasoning(description, task_type, project_id, tenant_id)

    impact, proactive, history, failures, graph = await asyncio.gather(
        impact_task,
        proactive_task,
        history_task,
        failure_task,
        graph_task,
        return_exceptions=True,
    )

    def _clean(value, fallback):
        return fallback if isinstance(value, Exception) else value

    impact = _clean(impact, {})
    proactive = _clean(proactive, {"relevant_code": [], "watch_out_for": []})
    history = _clean(history, [])
    failures = _clean(failures, [])
    graph = _clean(graph, None)

    prompt_sections: List[str] = []
    if graph and getattr(graph, "structural_context", ""):
        prompt_sections.append(f"[GRAPH] {graph.structural_context}")
    if graph and getattr(graph, "solution", None):
        prompt_sections.append(f"[REUSED REASONING] {graph.solution}")
    if impact:
        prompt_sections.append(f"[IMPACT] {json.dumps(impact, ensure_ascii=True)}")
    watch_out_for = list((proactive or {}).get("watch_out_for") or [])
    relevant_code = list((proactive or {}).get("relevant_code") or [])
    if relevant_code:
        prompt_sections.append(f"[RELEVANT CODE] {json.dumps(relevant_code[:5], ensure_ascii=True)}")
    if watch_out_for:
        prompt_sections.append(f"[WATCH OUT] {json.dumps(watch_out_for[:5], ensure_ascii=True)}")
    if history:
        prompt_sections.append(f"[SIMILAR SOLUTIONS] {json.dumps(history[:3], ensure_ascii=True)}")
    if failures:
        prompt_sections.append(f"[FAILURE CLUSTERS] {json.dumps(failures[:3], ensure_ascii=True)}")

    graph_payload = {
        "has_solution": bool(graph and getattr(graph, "solution", None)),
        "solution": getattr(graph, "solution", None) if graph else None,
        "confidence": float(getattr(graph, "confidence", 0.0) or 0.0) if graph else 0.0,
        "graph": ((getattr(graph, "graph_result", {}) or {}).get("sources") or []) if graph else [],
    }

    return {
        "enriched_system_prompt": "\n".join(prompt_sections).strip(),
        "intelligence": {
            "impact": impact,
            "proactive_context": proactive,
            "similar_past_solutions": history,
            "recent_failure_clusters": failures,
            "graph_reasoning": graph_payload,
        },
    }


async def enqueue_llm_retry(task_id: str, tenant_id: str, attempt: int = 0) -> dict[str, Any]:
    from services.streaming.core.redis_ import get_async_redis

    redis = get_async_redis()
    if not redis:
        return {"queued": False, "reason": "redis_unavailable"}
    delay = min(_LLM_RETRY_MAX_DELAY_S, _LLM_RETRY_BASE_DELAY_S * (2 ** max(attempt, 0)))
    score = time.time() + delay
    payload = json.dumps({"task_id": task_id, "attempt": attempt})
    await redis.zadd(_LLM_RETRY_QUEUE_KEY.format(tenant_id=tenant_id), {payload: score})
    return {"queued": True, "score": score, "delay_s": delay}


async def process_llm_retry_queue(tenant_id: str) -> list[dict[str, Any]]:
    from services.streaming.core.db import async_db
    from services.streaming.core.redis_ import get_async_redis

    redis = get_async_redis()
    if not redis:
        return []
    key = _LLM_RETRY_QUEUE_KEY.format(tenant_id=tenant_id)
    now = time.time()
    payloads = await redis.zrangebyscore(key, 0, now, start=0, num=50)
    requeued: list[dict[str, Any]] = []

    for payload in payloads:
        raw = payload.decode("utf-8") if isinstance(payload, (bytes, bytearray)) else str(payload)
        data = json.loads(raw)
        attempt = int(data.get("attempt", 0))
        task_id = str(data.get("task_id") or "")

        try:
            if attempt >= _LLM_RETRY_MAX_ATTEMPTS:
                # audit C5: fix dead-letter race condition.
                # Remove from Redis FIRST (atomic pipeline). If Postgres then fails, the task
                # remains non-dead-lettered in PG (safe — it will retry) rather than being
                # silently double-dead-lettered on the next scan.
                if hasattr(redis, "pipeline"):
                    pipe = redis.pipeline(transaction=True)
                    pipe.zrem(key, payload)
                    await pipe.execute()
                else:
                    await redis.zrem(key, payload)

                try:
                    async with async_db(tenant_id=tenant_id) as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "UPDATE tasks SET status = 'dead-letter' WHERE tenant_id = %s AND id = %s AND status != 'dead-letter'",
                                (tenant_id, task_id),
                            )
                except Exception as pg_exc:
                    log.error(
                        "dead_letter_pg_failed task_id=%s tenant=%s error=%s — task removed from retry queue but NOT marked dead-letter in DB",
                        task_id, tenant_id, pg_exc,
                    )
                continue

            await enqueue_llm_retry(task_id, tenant_id, attempt=attempt + 1)
            await redis.zrem(key, payload)
            requeued.append({"task_id": task_id, "attempt": attempt + 1})
        except Exception as e:
            log.error("retry_requeue_failed task_id=%s error=%s", task_id, e)
    return requeued


def _build_capability_snapshot(orch: CognitiveOrchestrator, init_attempted: bool) -> Dict[str, Any]:
    component_map = {
        "planner": orch.registry.get("planner"),
        "memory": orch.registry.get("memory"),
        "admission": orch.registry.get("admission"),
        "graph_intel": orch.registry.get("graph_intel"),
    }
    critical_components = {"planner", "memory"}
    components: Dict[str, str] = {}
    critical_missing: List[str] = []
    optional_missing: List[str] = []
    available = 0

    for name, component in component_map.items():
        status = "available" if component is not None else "missing"
        components[name] = status
        if status == "available":
            available += 1
        elif name in critical_components:
            critical_missing.append(name)
        else:
            optional_missing.append(name)

    total = max(len(component_map), 1)
    score = round(available / total, 2)
    if not orch._initialized:
        quality_status = "limited"
        summary = "orchestrator not fully initialized"
    elif critical_missing:
        quality_status = "limited"
        summary = f"critical gaps: {', '.join(critical_missing)}"
    elif optional_missing:
        quality_status = "degraded"
        summary = f"optional gaps: {', '.join(optional_missing)}"
    else:
        quality_status = "full"
        summary = "all critical cognitive components available"

    return {
        "initialized": orch._initialized,
        "init_attempted": init_attempted,
        "quality_status": quality_status,
        "score": score,
        "critical_missing": critical_missing,
        "optional_missing": optional_missing,
        "components": components,
        "summary": summary,
    }


async def get_cognitive_capability_snapshot_async(force_init: bool = False) -> Dict[str, Any]:
    orch = get_orchestrator()
    init_attempted = bool(orch._initialized or force_init)
    if force_init and not orch._initialized:
        try:
            await orch.initialize()
        except Exception as exc:
            log.warning("capability_snapshot_async_init_failed error=%s", exc)
    return _build_capability_snapshot(orch, init_attempted)


def get_cognitive_capability_snapshot(force_init: bool = False) -> Dict[str, Any]:
    """
    Best-effort synchronous capability snapshot used by health/readiness routes.
    When called from a running event loop, force_init degrades to inspection only.
    """
    orch = get_orchestrator()
    init_attempted = bool(orch._initialized or force_init)
    if force_init and not orch._initialized:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                asyncio.run(orch.initialize())
            except Exception as exc:
                log.warning("capability_snapshot_init_failed error=%s", exc)
    return _build_capability_snapshot(orch, init_attempted)

async def orchestrator_shutdown():
    if _instance:
        await _instance.shutdown()
