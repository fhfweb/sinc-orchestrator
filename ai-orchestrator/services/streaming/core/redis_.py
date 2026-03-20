"""
streaming/core/redis_.py
========================
Redis connection, sliding-window rate limiter, ask-response cache,
and daily token usage counters (atomic, cluster-safe).
"""
import datetime as _dt
import hashlib
import json
import logging
import threading
import time
from collections import defaultdict, deque

from .config import REDIS_HOST, REDIS_PORT, REDIS_DB, REDIS_PASSWORD, ASK_CACHE_TTL, RATE_LIMIT_FAIL_OPEN

log = logging.getLogger("orchestrator")

# ── Connection ─────────────────────────────────────────────────────────────────

_async_redis = None


def get_redis():
    global _redis
    if _redis is not None:
        return _redis
    try:
        import redis as _r
        _redis = _r.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            password=REDIS_PASSWORD,
            decode_responses=True, socket_connect_timeout=2,
        )
        _redis.ping()
    except Exception:
        _redis = None
    return _redis


def get_async_redis():
    global _async_redis
    if _async_redis is not None:
        return _async_redis
    try:
        import redis.asyncio as _r
        _async_redis = _r.Redis(
            host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
            password=REDIS_PASSWORD,
            decode_responses=True, socket_connect_timeout=2,
        )
        return _async_redis
    except Exception:
        return None


# ── Sliding-window rate limiter ────────────────────────────────────────────────

_rate_windows: dict[str, deque] = defaultdict(deque)
_rate_lock = threading.Lock()


def check_rate_limit(tenant_id: str, limit_rpm: int) -> bool:
    """Return True if request is allowed, False if rate-limited."""
    now    = time.time()
    window = 60.0
    r = get_redis()
    if r:
        key = f"rl:{tenant_id}"
        try:
            pipe = r.pipeline()
            pipe.zremrangebyscore(key, 0, now - window)
            pipe.zadd(key, {str(now): now})
            pipe.zcard(key)
            pipe.expire(key, 120)
            results = pipe.execute()
            return results[2] <= limit_rpm
        except Exception:
            pass  # Redis error — fall through to in-memory
    # Redis unavailable — behaviour is governed by RATE_LIMIT_FAIL_OPEN:
    #   false (default) → fail-safe: deny the request (returns False → 429)
    #   true            → fall back to per-instance in-memory window (split-brain risk)
    if not RATE_LIMIT_FAIL_OPEN:
        log.warning("rate_limit_fail_safe tenant=%s redis_unavailable", tenant_id)
        return False
    log.warning("rate_limit_fallback_to_memory tenant=%s redis_unavailable", tenant_id)
    with _rate_lock:
        dq     = _rate_windows[tenant_id]
        cutoff = now - window
        while dq and dq[0] < cutoff:
            dq.popleft()
        if len(dq) >= limit_rpm:
            return False
        dq.append(now)
        return True


async def async_check_rate_limit(tenant_id: str, limit_rpm: int) -> bool:
    """Async version of check_rate_limit."""
    now    = time.time()
    window = 60.0
    r = get_async_redis()
    if r:
        key = f"rl:{tenant_id}"
        try:
            async with r.pipeline(transaction=True) as pipe:
                await pipe.zremrangebyscore(key, 0, now - window)
                await pipe.zadd(key, {str(now): now})
                await pipe.zcard(key)
                await pipe.expire(key, 120)
                results = await pipe.execute()
                return results[2] <= limit_rpm
        except Exception:
            pass
    # Mirror the sync fallback policy (RATE_LIMIT_FAIL_OPEN).
    if not RATE_LIMIT_FAIL_OPEN:
        log.warning("rate_limit_fail_safe tenant=%s redis_unavailable", tenant_id)
        return False
    return check_rate_limit(tenant_id, limit_rpm)


# ── Ask-response cache ─────────────────────────────────────────────────────────

def project_cache_version(tenant_id: str, project_id: str) -> str:
    r = get_redis()
    if not r:
        return "0"
    try:
        v = r.get(f"cache_ver:{tenant_id}:{project_id}")
        return v or "0"
    except Exception:
        return "0"


def invalidate_project_cache(tenant_id: str, project_id: str):
    r = get_redis()
    if not r:
        return
    try:
        key = f"cache_ver:{tenant_id}:{project_id}"
        r.incr(key)
        r.expire(key, 86400 * 30)
    except Exception:
        pass


# ── Distributed Leaderboard (Module 4.3) ───────────────────────────────────

async def async_update_agent_leaderboard(tenant_id: str, task_type: str, agent_name: str, success: bool):
    """
    Update the real-time agent leaderboard using an EMA-proxy.
    Success: +1.0, Failure: -0.5.
    """
    r = get_async_redis()
    if not r: return
    lb_key = f"sinc:leaderboard:{tenant_id}:{task_type}"
    # EMA update proxy: zincrby with decay is complex in Redis alone, 
    # so we use frequent small updates.
    score_delta = 1.0 if success else -0.5
    try:
        await r.zincrby(lb_key, score_delta, agent_name)
    except Exception as e:
        log.warning("leaderboard_update_failed error=%s", e)


def agent_reputation_key(agent_name: str, tenant_id: str | None = None) -> str:
    """Return the canonical Redis hash key for agent reputation."""
    normalized_agent = str(agent_name or "none").strip() or "none"
    normalized_tenant = str(tenant_id or "").strip()
    if normalized_tenant:
        return f"agent:rep:{normalized_tenant}:{normalized_agent}"
    return f"agent:rep:{normalized_agent}"


async def async_get_agent_reputation_score(
    agent_name: str,
    tenant_id: str | None = None,
    *,
    default: float = 0.5,
) -> float:
    """
    Read the tenant-scoped reputation signal with fallback to the legacy key.

    This keeps the runtime compatible with older Redis state while new writes
    stay isolated per tenant.
    """
    r = get_async_redis()
    if not r:
        return default

    candidate_keys: list[str] = []
    normalized_tenant = str(tenant_id or "").strip()
    if normalized_tenant:
        candidate_keys.append(agent_reputation_key(agent_name, normalized_tenant))
    candidate_keys.append(agent_reputation_key(agent_name))

    for key in candidate_keys:
        try:
            raw = await r.hget(key, "score")
            if raw is not None:
                return float(raw)
        except Exception:
            continue
    return default


async def async_update_agent_reputation_hash(
    tenant_id: str,
    task_type: str,
    agent_name: str,
    success: bool,
    *,
    duration_ms: int = 0,
    alpha: float = 0.15,
) -> None:
    """Update the tenant-scoped Redis reputation hash for an agent."""
    r = get_async_redis()
    if not r:
        return

    key = agent_reputation_key(agent_name, tenant_id)
    try:
        prev_raw = await r.hget(key, "score")
        prev_score = float(prev_raw) if prev_raw is not None else 0.5
        observation = 1.0 if success else 0.0
        new_score = round((prev_score * (1.0 - alpha)) + (observation * alpha), 4)
        samples_raw = await r.hget(key, "samples")
        samples = int(samples_raw or 0) + 1
        await r.hset(
            key,
            mapping={
                "score": new_score,
                "samples": samples,
                "tenant_id": tenant_id,
                "task_type": task_type,
                "last_duration_ms": int(duration_ms or 0),
                "updated_at": _dt.datetime.utcnow().isoformat(),
            },
        )
    except Exception as exc:
        log.warning("agent_reputation_hash_update_failed error=%s", exc)

# ── Smart Pub/Sub Invalidation (Module 4.2) ───────────────────────────────

def notify_file_change(tenant_id: str, project_id: str, file_path: str):
    """
    Publish a message to invalidate caches related to a changed file.
    All cluster listeners will reset their local/semantic caches for this scope.
    """
    r = get_redis()
    if not r: return
    channel = f"sinc:invalidations:{tenant_id}"
    msg = json.dumps({
        "project_id": project_id,
        "file_path": file_path,
        "type": "file_change",
        "timestamp": time.time()
    })
    try:
        r.publish(channel, msg)
        # Also increment version to force-break standard ask-response caches
        invalidate_project_cache(tenant_id, project_id)
    except Exception as e:
        log.warning("pubsub_notification_failed error=%s", e)


# ── Intelligent Retry Queue (Module 4.1) ───────────────────────────────────

RETRY_QUEUE_PREFIX = "sinc:llm_retry:"
RETRY_BACKOFF_KEY = "sinc:llm_backoff_active"

async def enqueue_llm_retry(tenant_id: str, task_id: str, attempt: int = 1):
    """
    Enqueues a task for LLM retry using a sorted set for scheduling.
    Backoff: 5s * 2^(attempt-1), max 300s.
    """
    r = get_async_redis()
    if not r: return
    
    backoff = min(300, 5 * (2 ** (attempt - 1)))
    retry_at = time.time() + backoff
    key = f"{RETRY_QUEUE_PREFIX}{tenant_id}"
    
    try:
        pipe = r.pipeline()
        pipe.zadd(key, {task_id: retry_at})
        # Set a global flag that retries are pending
        pipe.setex(RETRY_BACKOFF_KEY, 300, "1")
        await pipe.execute()
        log.info("llm_retry_queued task=%s tenant=%s attempt=%d retry_in=%ds", 
                 task_id, tenant_id, attempt, backoff)
    except Exception as e:
        log.warning("llm_retry_failed_to_queue error=%s", e)

async def async_get_ready_retries(tenant_id: str, limit: int = 10) -> list[str]:
    """Fetch task IDs that are ready for retry."""
    r = get_async_redis()
    if not r: return []
    key = f"{RETRY_QUEUE_PREFIX}{tenant_id}"
    now = time.time()
    try:
        # Get tasks with score <= now
        tasks = await r.zrangebyscore(key, 0, now, start=0, num=limit)
        if tasks:
            # Remove them so they don't get picked up multiple times
            await r.zrem(key, *tasks)
        return tasks
    except Exception as e:
        log.warning("fetch_retries_failed error=%s", e)
        return []


def cache_key(tenant_id: str, prompt: str, project_id: str) -> str:
    ver = project_cache_version(tenant_id, project_id)
    h   = hashlib.sha256(f"{tenant_id}|{project_id}|{ver}|{prompt}".encode()).hexdigest()[:24]
    return f"ask:{h}"


def cache_get(key: str) -> dict | None:
    r = get_redis()
    if not r:
        return None
    try:
        raw = r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def cache_set(key: str, value: dict):
    r = get_redis()
    if not r:
        return
    try:
        r.setex(key, ASK_CACHE_TTL, json.dumps(value))
    except Exception:
        pass


# ── Daily token usage counters (fix for audit risk #3: O(N) quota) ────────────
# Each tenant has one key per UTC day: tokens_today:{tenant_id}
# Updated atomically via INCRBY — works across all cluster instances.

_TOKEN_KEY_PREFIX = "tokens_today:"


def _today_expiry_seconds() -> int:
    """Seconds until midnight UTC. Used as Redis TTL so keys auto-expire."""
    now      = _dt.datetime.now(_dt.timezone.utc)
    midnight = (now + _dt.timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return max(int((midnight - now).total_seconds()), 1)


def incr_token_usage(tenant_id: str, tokens: int) -> None:
    """Atomically add tokens to the tenant's daily counter."""
    if tokens <= 0 or not tenant_id:
        return
    r = get_redis()
    if not r:
        return
    key = f"{_TOKEN_KEY_PREFIX}{tenant_id}"
    try:
        pipe = r.pipeline()
        pipe.incrby(key, tokens)
        pipe.expire(key, _today_expiry_seconds())
        pipe.execute()
    except Exception:
        pass


async def async_incr_token_usage(tenant_id: str, tokens: int) -> None:
    """Async version of incr_token_usage."""
    if tokens <= 0 or not tenant_id:
        return
    r = get_async_redis()
    if not r:
        return
    key = f"{_TOKEN_KEY_PREFIX}{tenant_id}"
    try:
        async with r.pipeline(transaction=True) as pipe:
            await pipe.incrby(key, tokens)
            await pipe.expire(key, _today_expiry_seconds())
            await pipe.execute()
    except Exception:
        pass


def get_token_usage_today(tenant_id: str) -> int:
    """Return the daily token count from Redis. Returns -1 if Redis is unavailable."""
    r = get_redis()
    if not r:
        return -1
    try:
        val = r.get(f"{_TOKEN_KEY_PREFIX}{tenant_id}")
        return int(val) if val is not None else 0
    except Exception:
        return -1


async def async_get_token_usage_today(tenant_id: str) -> int:
    """Async version of get_token_usage_today."""
    r = get_async_redis()
    if not r:
        return -1
    try:
        val = await r.get(f"{_TOKEN_KEY_PREFIX}{tenant_id}")
        return int(val) if val is not None else 0
    except Exception:
        return -1
