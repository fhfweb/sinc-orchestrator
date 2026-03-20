from services.streaming.core.config import env_get
"""
streaming/routes/ask.py
=======================
FastAPI Router for Natural-language queries (RAG).
"""
import json
import logging
import os
import asyncio
import time
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status, Response, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.http_client import create_resilient_client
from services.streaming.core.auth import (
    get_tenant_id, get_tenant,
    check_quota,
    enqueue_webhook,
    log_usage_async,
)
from services.streaming.core.billing import check_backend_access
from services.streaming.core.circuit import get_breaker
from services.streaming.core.security_config import safe_project_path
from services.streaming.core.redis_ import async_get_token_usage_today
from services.streaming.core.sse import broadcast

log = logging.getLogger("orchestrator")

router = APIRouter(tags=["ask"])

# ── Models ───────────────────────────────────────────────────────────────────

class AskRequest(BaseModel):
    prompt: str
    project_id: Optional[str] = ""
    session_id: Optional[str] = ""
    project_path: Optional[str] = None
    force_no_cache: bool = False

# ── Lazy memory router (shared singleton) ─────────────────────────────────────

_memory_router = None

def _get_memory_router():
    global _memory_router
    if _memory_router is not None:
        return _memory_router
    try:
        from services.streaming.core.config import DB_CONFIG, REDIS_HOST, REDIS_PORT, REDIS_DB
        from services.memory_layers import (
            L0RuleEngine,
            L1DeterministicCache,
            L2SemanticMemory,
            L3GraphReasoning,
            L4EventMemory,
            MemoryHierarchyRouter,
        )

        l0 = L0RuleEngine()
        l1 = L1DeterministicCache(
            redis_url=f"redis://{REDIS_HOST}:{REDIS_PORT}/{REDIS_DB}"
        )

        def _noop_embedder(_text): return None

        l2 = L2SemanticMemory(
            qdrant_host=env_get("QDRANT_HOST", default="localhost"),
            qdrant_port=int(env_get("QDRANT_PORT", default="6333")),
            embedder_func=_noop_embedder,
        )

        neo4j_uri  = env_get("NEO4J_URI", default="")
        neo4j_user = env_get("NEO4J_USER", default="neo4j")
        neo4j_pass = env_get("NEO4J_PASSWORD", default="")
        l3 = L3GraphReasoning(neo4j_uri, neo4j_user, neo4j_pass) if neo4j_uri else None
        l4 = L4EventMemory(DB_CONFIG)

        _memory_router = MemoryHierarchyRouter(l0=l0, l1=l1, l2=l2, l3=l3, l4=l4)
    except Exception as exc:
        log.warning("memory_router_init_failed error=%s", exc)
        _memory_router = None
    return _memory_router

# ── Helpers ───────────────────────────────────────────────────────────────────

def _get_redis():
    from services.streaming.core.redis_ import get_async_redis
    return get_async_redis()

async def _load_session(tenant_id: str, session_id: str) -> List[Dict[str, str]]:
    if not session_id: return []
    r = _get_redis()
    if not r: return []
    try:
        raw = await r.get(f"session:{tenant_id}:{session_id}")
        return json.loads(raw) if raw else []
    except Exception:
        return []

async def _save_session(tenant_id: str, session_id: str, history: List[Dict[str, str]], prompt: str, answer: str):
    if not session_id: return
    r = _get_redis()
    if not r: return
    try:
        updated = (history + [
            {"role": "user",      "content": prompt},
            {"role": "assistant", "content": answer},
        ])[-20:]
        await r.setex(f"session:{tenant_id}:{session_id}", 3600, json.dumps(updated))
    except Exception:
        log.debug("redis_session_save_error", exc_info=True)

def _route_prompt(prompt: str) -> Dict[str, Any]:
    try:
        from services.inference_router import route_prompt
        return route_prompt(prompt)
    except ImportError:
        return {
            "tier":    "medium",
            "model":   env_get("OLLAMA_MODEL_GENERAL", default="qwen2.5-coder:14b-instruct-q4_K_M"),
            "backend": "ollama",
            "agents":  [],
            "intent":  "unknown",
        }

async def _retrieve_context(prompt: str, project_id: str, tenant_id: str, project_path: str = "") -> tuple:
    breaker = get_breaker("qdrant")
    try:
        from services.context_retriever import graph_aware_retrieve
        # Level 5: Use the async, cognitively-aware, ranked and summarized version
        ctx = await breaker.call(graph_aware_retrieve, prompt, project_id=project_id, tenant_id=tenant_id)
        return ctx.get("context", ""), ctx.get("sources", [])
    except Exception:
        log.warning("context_retrieval_error", exc_info=True)
        return "", []

def _build_system_prompt(context_text: str) -> str:
    base = (
        "You are an expert software engineer assistant. "
        "Answer the user's question about the codebase using the provided context. "
        "Be concise and precise. Reference specific files and line numbers when relevant."
    )
    if context_text:
        # Note: Truncation is now handled intelligently by ContextEngine during formatting.
        base += f"\n\nCODEBASE CONTEXT:\n{context_text}"
    return base

async def _call_llm_async(routing: Dict[str, Any], system_prompt: str, messages: List[Dict[str, str]]) -> tuple:
    """Async call to Anthropic or sync call to Ollama."""
    if routing["backend"] == "anthropic":
        import anthropic
        client = anthropic.AsyncAnthropic(api_key=env_get("ANTHROPIC_API_KEY", default=""))
        msg = await client.messages.create(
            model=routing["model"], max_tokens=2048,
            system=system_prompt,
            messages=messages,
        )
        return msg.content[0].text, msg.usage.input_tokens, msg.usage.output_tokens
    else:
        ollama_host = env_get("OLLAMA_HOST", default="http://localhost:11434")
        async with create_resilient_client(
            service_name="ask",
            timeout=120.0,
        ) as client:
            resp = await client.post(
                f"{ollama_host}/api/chat",
                json={
                    "model":    routing["model"],
                    "messages": [{"role": "system", "content": system_prompt}] + messages,
                    "stream":   False,
                }
            )
            result = resp.json()
            return result.get("message", {}).get("content", ""), 0, 0

# ── Routes ────────────────────────────────────────────────────────────────────

@router.post("/ask")
async def ask(
    body: AskRequest,
    tenant_id: str = Depends(get_tenant_id),
    current_tenant_obj: Dict[str, Any] = Depends(get_tenant),
    cache_control: Optional[str] = Header(None, alias="Cache-Control"),
):
    """Natural-language query with full cognitive pipeline."""
    t_start = time.monotonic()

    # Quota check (raises HTTP 402 if exceeded)
    await check_quota(current_tenant_obj, tokens_needed=1000)

    # ── Cognitive Memory Hierarchy (L0→L4) ────────────────────────────────────
    no_cache = body.force_no_cache or cache_control == "no-cache"
    if not no_cache:
        m_router = _get_memory_router()
        if m_router:
            try:
                hit = await asyncio.to_thread(
                    m_router.resolve,
                    task_type="ask",
                    description=body.prompt,
                    project_id=body.project_id,
                    tenant_id=tenant_id
                )
                if hit:
                    hit["cached"] = True
                    # Increment Prometheus-readable cache-hit counter in Redis
                    try:
                        _r = _get_redis()
                        if _r:
                            await _r.incr(f"ask_cache_hits:{tenant_id}")
                    except Exception:
                        pass
                    return hit
            except Exception:
                log.warning("memory_router_resolve_error", exc_info=True)

    # ── Routing & Context ─────────────────────────────────────────────────────
    plan    = current_tenant_obj.get("plan", "free")
    routing = _route_prompt(body.prompt)
    if not check_backend_access(plan, routing.get("backend", "ollama")):
        routing["backend"] = "ollama"
        routing["model"]   = env_get("OLLAMA_MODEL_GENERAL", default="qwen2.5:7b-instruct-q4_K_M")

    raw_path = body.project_path or env_get("AGENT_WORKSPACE", default="")
    try:
        project_path = safe_project_path(raw_path) if raw_path else ""
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    context_text, sources = await _retrieve_context(body.prompt, body.project_id, tenant_id, project_path)
    system_prompt = _build_system_prompt(context_text)

    # ── LLM Call ──────────────────────────────────────────────────────────────
    chat_history = await _load_session(tenant_id, body.session_id)
    llm_messages = chat_history + [{"role": "user", "content": body.prompt}]

    try:
        answer, tokens_in, tokens_out = await _call_llm_async(routing, system_prompt, llm_messages)
    except Exception as llm_err:
        log.error(f"llm_call_failed: {llm_err}")
        raise HTTPException(status_code=502, detail=f"LLM call failed: {llm_err}")

    await _save_session(tenant_id, body.session_id, chat_history, body.prompt, answer)
    latency_ms = int((time.monotonic() - t_start) * 1000)

    # ── Side effects ──────────────────────────────────────────────────────────
    from fastapi import BackgroundTasks
    bg = BackgroundTasks()
    bg.add_task(log_usage_async, endpoint="/ask", tier=routing["tier"], model=routing["model"],
                tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms, tenant_id=tenant_id)
    
    await broadcast("ask_completed", {
        "project_id": body.project_id,
        "intent":     routing.get("intent"),
        "tier":       routing["tier"],
        "latency_ms": latency_ms,
    }, tenant_id=tenant_id)

    # Learn
    m_router = _get_memory_router()
    if m_router:
        bg.add_task(m_router.learn, task_type="ask", description=body.prompt, solution=answer,
                    project_id=body.project_id, tenant_id=tenant_id, success=True, tokens_used=tokens_in + tokens_out)

    response_body = {
        "answer":     answer,
        "sources":    sources,
        "tier":       routing["tier"],
        "model":      routing["model"],
        "intent":     routing.get("intent"),
        "agents":     routing.get("agents", []),
        "latency_ms": latency_ms,
        "session_id": body.session_id or None,
    }
    
    enqueue_webhook(tenant_id, "ask.done", {
        "project_id": body.project_id, "prompt": body.prompt[:200],
        "intent": routing.get("intent"), "tier": routing["tier"], "latency_ms": latency_ms,
    })

    _limit = current_tenant_obj.get("tokens_per_day", 500_000)
    _used  = await async_get_token_usage_today(tenant_id) + tokens_out
    
    return Response(
        content=json.dumps(response_body),
        status_code=200,
        headers={
            "X-Quota-Remaining": str(max(0, _limit - _used)),
            "X-Quota-Limit":     str(_limit),
        },
        media_type="application/json",
        background=bg
    )

@router.get("/ask/stream")
async def ask_stream(
    prompt: str = Query(...),
    project_id: str = "",
    session_id: str = "",
    tenant_id: str = Depends(get_tenant_id),
    current_tenant_obj: Dict[str, Any] = Depends(get_tenant),
):
    """SSE endpoint: streams LLM tokens."""
    allowed, reason = check_quota(current_tenant_obj, tokens_needed=500)
    if not allowed:
        raise HTTPException(status_code=429, detail=reason)

    plan    = current_tenant_obj.get("plan", "free")
    routing = _route_prompt(prompt)
    if not check_backend_access(plan, routing.get("backend", "ollama")):
        routing["backend"] = "ollama"
        routing["model"]   = env_get("OLLAMA_MODEL_GENERAL", default="qwen2.5:7b-instruct-q4_K_M")

    context_text, _ = await _retrieve_context(prompt, project_id, tenant_id)
    system_prompt = _build_system_prompt(context_text)

    stream_history = await _load_session(tenant_id, session_id)
    llm_messages = stream_history + [{"role": "user", "content": prompt}]
    t_start = time.monotonic()

    async def _event_generator():
        full_answer = []
        tokens_in = tokens_out = 0
        try:
            if routing["backend"] == "anthropic":
                import anthropic
                client = anthropic.AsyncAnthropic(api_key=env_get("ANTHROPIC_API_KEY", default=""))
                async with client.messages.stream(
                    model=routing["model"], max_tokens=2048,
                    system=system_prompt,
                    messages=llm_messages,
                ) as stream:
                    async for text in stream.text_stream:
                        full_answer.append(text)
                        yield f"data: {json.dumps({'token': text})}\n\n"
                
                usage = await stream.get_final_message()
                tokens_in = usage.usage.input_tokens
                tokens_out = usage.usage.output_tokens
            else:
                ollama_host = env_get("OLLAMA_HOST", default="http://localhost:11434")
                async with create_resilient_client(
                    service_name="ask-stream",
                    timeout=120.0,
                ) as client:
                    async with client.stream(
                        "POST", f"{ollama_host}/api/chat",
                        json={
                            "model":    routing["model"],
                            "messages": [{"role": "system", "content": system_prompt}] + llm_messages,
                            "stream":   True,
                        }
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if not line.strip(): continue
                            chunk = json.loads(line)
                            token = chunk.get("message", {}).get("content", "")
                            if token:
                                full_answer.append(token)
                                yield f"data: {json.dumps({'token': token})}\n\n"
                                tokens_out += 1

            await _save_session(tenant_id, session_id, stream_history, prompt, "".join(full_answer))
            latency_ms = int((time.monotonic() - t_start) * 1000)
            yield f"data: {json.dumps({'done': True, 'latency_ms': latency_ms, 'session_id': session_id or None})}\n\n"
            
            await log_usage_async(endpoint="/ask/stream", tier=routing["tier"], model=routing["model"],
                                  tokens_in=tokens_in, tokens_out=tokens_out, latency_ms=latency_ms, tenant_id=tenant_id)
        except Exception as exc:
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
