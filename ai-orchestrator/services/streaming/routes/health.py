from services.streaming.core.config import env_get
"""
streaming/routes/health.py
==========================
GET /health         — shallow health (fast path, no I/O)
GET /health/deep    — deep health: probes all memory-hierarchy layers and
                      infrastructure services with 500 ms per-check timeouts.

Memory layer naming in the deep response:
  l0_redis    — Redis (rate-limit cache, heartbeat TTLs, SSE conn tracking)
  l1_postgres — PostgreSQL (primary task / tenant store)
  l2_neo4j    — Neo4j / Digital Twin graph (optional)
  l3_qdrant   — Qdrant vector store (optional semantic memory)
  l4_llm      — LLM endpoint reachability (Ollama or Anthropic)
  event_bus   — Redis pub/sub coordination bus
  llm_semaphore — live LLM concurrency slot usage
"""
import asyncio
import os
from fastapi import APIRouter, Response, status

from services.http_client import create_resilient_client
from services.streaming.core.auth import now_iso
from services.streaming.core.circuit import _breakers          # registry of live breakers
from services.streaming.core.db import async_db

router = APIRouter(tags=["health"])

# Per-check probe timeout (seconds).  Keeps /health/deep bounded under load.
_PROBE_TIMEOUT_S = 0.5


async def _probe(coro, timeout: float = _PROBE_TIMEOUT_S) -> str:
    """Run *coro* with a hard timeout; return 'ok' or an error string."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        return f"timeout after {timeout}s"
    except Exception as exc:
        return f"error: {exc}"


# ── Individual probes ─────────────────────────────────────────────────────────

async def _probe_postgres() -> str:
    async with async_db() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT 1")
    return "ok"


async def _probe_redis() -> str:
    from services.streaming.core.redis_ import get_async_redis
    r = get_async_redis()
    if not r:
        return "unavailable"
    await r.ping()
    return "ok"


async def _probe_neo4j() -> str:
    uri = env_get("NEO4J_URI", default="")
    if not uri:
        return "not_configured"
    host = uri.replace("bolt://", "").replace("neo4j://", "").split(":")[0]
    async with create_resilient_client(
        service_name="health-probe",
        timeout=_PROBE_TIMEOUT_S,
    ) as client:
        await client.get(f"http://{host}:7474", timeout=_PROBE_TIMEOUT_S)
    return "ok"


async def _probe_qdrant() -> str:
    host = env_get("QDRANT_HOST", default="")
    if not host:
        return "not_configured"
    port = env_get("QDRANT_PORT", default="6333")
    async with create_resilient_client(
        service_name="health-probe",
        timeout=_PROBE_TIMEOUT_S,
    ) as client:
        await client.get(f"http://{host}:{port}/healthz", timeout=_PROBE_TIMEOUT_S)
    return "ok"


async def _probe_llm() -> str:
    ollama = env_get("OLLAMA_HOST", default="")
    if ollama:
        async with create_resilient_client(
            service_name="health-probe",
            timeout=_PROBE_TIMEOUT_S,
        ) as client:
            await client.get(f"{ollama}/api/tags", timeout=_PROBE_TIMEOUT_S)
        return "ok"
    if env_get("ANTHROPIC_API_KEY"):
        return "configured"
    return "not_configured"


async def _probe_event_bus() -> str:
    from services.event_bus import EventBus
    bus = await EventBus.get_instance()
    return "ok" if bus._connected else "disconnected"


# ── Routes ────────────────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    """Shallow health (fast)."""
    return {"status": "ok", "ts": now_iso()}

@router.get("/health/deep")
async def health_deep(response: Response):
    """
    Deep health check with 500 ms per-probe timeouts.

    Critical layers (l1_postgres, event_bus) set the overall status to
    'degraded' and return HTTP 503 if unavailable.
    Optional layers (l0_redis, l2_neo4j, l3_qdrant, l4_llm) are reported
    but do not affect the HTTP status code.
    """
    # Run all layer probes concurrently for speed.
    (l0, l1, l2, l3, l4, eb) = await asyncio.gather(
        _probe(_probe_redis()),
        _probe(_probe_postgres()),
        _probe(_probe_neo4j()),
        _probe(_probe_qdrant()),
        _probe(_probe_llm()),
        _probe(_probe_event_bus()),
    )

    layers = {
        "l0_redis":    l0,
        "l1_postgres": l1,
        "l2_neo4j":    l2,
        "l3_qdrant":   l3,
        "l4_llm":      l4,
        "event_bus":   eb,
    }

    # LLM semaphore: report free slots (non-blocking)
    try:
        from services.cognitive_orchestrator import _llm_semaphore, LLM_MAX_CONCURRENCY
        in_flight = LLM_MAX_CONCURRENCY - _llm_semaphore._value  # type: ignore[attr-defined]
        layers["llm_semaphore"] = f"{in_flight}/{LLM_MAX_CONCURRENCY} in flight"
    except Exception:
        layers["llm_semaphore"] = "unavailable"

    cognitive = {
        "initialized": False,
        "init_attempted": False,
        "quality_status": "unavailable",
        "score": 0.0,
        "critical_missing": ["cognitive_orchestrator"],
        "optional_missing": [],
        "components": {},
        "summary": "cognitive capability snapshot unavailable",
    }

    # Cognitive orchestrator warm-up state
    try:
        from services.cognitive_orchestrator import get_cognitive_capability_snapshot_async

        cognitive = await get_cognitive_capability_snapshot_async(force_init=True)
        layers["cognitive_orchestrator"] = cognitive.get("quality_status", "unknown")
    except Exception as exc:
        layers["cognitive_orchestrator"] = f"error: {exc}"
        cognitive["summary"] = f"snapshot failed: {exc}"

    # Ollama legacy alias (kept for backward compat with existing dashboards)
    layers["ollama"] = l4

    # Circuit breaker states
    circuit_states: dict[str, str] = {
        name: breaker.state.value for name, breaker in _breakers.items()
    }

    # Live SSE connection counts (per tenant, from Redis SETs)
    sse_connections: dict[str, int] = {}
    try:
        from services.streaming.core.redis_ import get_async_redis
        r = get_async_redis()
        if r:
            keys = await r.keys("sse_conns:*")
            for key in keys:
                tenant_id = key.split("sse_conns:", 1)[-1]
                sse_connections[tenant_id] = await r.scard(key)
    except Exception:
        pass

    # Determine overall status: critical layers must be "ok"
    critical_ok = l1 == "ok" and eb in ("ok", "disconnected")
    cognitive_ok = cognitive.get("quality_status") not in {"limited", "unavailable"}
    ok = critical_ok and cognitive_ok

    if not ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return {
        "status":           "ok" if ok else "degraded",
        "quality":          "full" if critical_ok and cognitive.get("quality_status") == "full" else "degraded",
        "layers":           layers,
        # legacy key kept for dashboards that read "checks"
        "checks":           layers,
        "cognitive":        cognitive,
        "circuit_breakers": circuit_states,
        "sse_connections": {
            "total":     sum(sse_connections.values()),
            "by_tenant": sse_connections,
        },
        "ts": now_iso(),
    }
