from services.streaming.core.config import env_get
"""
GPU Scheduler — Redis-based mutex for Ollama GPU access.

Ensures only one agent uses the GPU at a time.
When a second agent tries to acquire the GPU, it waits in queue
rather than causing VRAM contention or OOM errors.

On RTX 5070 (12–16 GB VRAM), loading more than one large model
simultaneously causes swapping to RAM — this prevents that.

Usage:
    from services.gpu_scheduler import acquire_gpu, release_gpu, GpuLock

    # Context manager (recommended):
    with GpuLock(agent_name="ai engineer", timeout=600):
        result = run_ollama(...)

    # Manual:
    token = acquire_gpu("my-agent", timeout=300)
    try:
        ...
    finally:
        release_gpu(token)
"""

import os
import time
import uuid
from contextlib import contextmanager

# Redis connection settings
REDIS_HOST = env_get("REDIS_HOST", default="redis")
REDIS_PORT = int(env_get("REDIS_PORT", default="6379"))
REDIS_DB   = int(env_get("REDIS_GPU_DB", default="1"))   # DB 1 for GPU scheduling

# Lock parameters
GPU_LOCK_KEY     = "sinc:gpu:lock"
GPU_LOCK_TTL     = 900     # 15 min max hold time (safety expiry)
GPU_QUEUE_KEY    = "sinc:gpu:queue"
POLL_INTERVAL    = 2.0     # seconds between lock retry attempts
DEFAULT_TIMEOUT  = 600     # wait at most 10 min for GPU


def _redis():
    """Return a Redis connection (lazy import — not available at module load)."""
    try:
        import redis
        return redis.Redis(host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                           decode_responses=True, socket_connect_timeout=5)
    except ImportError:
        # redis-py not installed — fall back to no-op scheduler
        return None


def _log(msg: str):
    from datetime import datetime, timezone
    print(f"[{datetime.now(timezone.utc).isoformat()}] [gpu-sched] {msg}", flush=True)


def acquire_gpu(agent_name: str, timeout: int = DEFAULT_TIMEOUT) -> str | None:
    """
    Acquire the GPU mutex. Returns a unique token to use when releasing.
    Returns None if Redis is unavailable (non-blocking fallback).
    Raises TimeoutError if wait exceeds timeout.
    """
    r = _redis()
    if r is None:
        _log("Redis unavailable — running without GPU lock (potential VRAM contention)")
        return None

    token = f"{agent_name}:{uuid.uuid4().hex[:8]}"
    deadline = time.monotonic() + timeout
    waited = 0.0

    while True:
        # SET NX EX — atomic "set if not exists with TTL"
        acquired = r.set(GPU_LOCK_KEY, token, nx=True, ex=GPU_LOCK_TTL)
        if acquired:
            _log(f"GPU acquired by {agent_name} (token={token[:20]})")
            return token

        current_holder = r.get(GPU_LOCK_KEY)
        if waited % 30 < POLL_INTERVAL:   # log every ~30s
            _log(f"GPU busy (held by {current_holder}) — {agent_name} waiting... ({int(waited)}s)")

        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"[gpu-sched] Timeout waiting for GPU after {timeout}s "
                f"(current holder: {current_holder})"
            )

        time.sleep(POLL_INTERVAL)
        waited += POLL_INTERVAL


def release_gpu(token: str | None):
    """Release the GPU mutex. Only releases if token matches current holder."""
    if token is None:
        return

    r = _redis()
    if r is None:
        return

    current = r.get(GPU_LOCK_KEY)
    if current == token:
        r.delete(GPU_LOCK_KEY)
        _log(f"GPU released (token={token[:20]})")
    else:
        _log(f"GPU release skipped — token mismatch (held={current}, mine={token[:20]})")


def extend_gpu_lease(token: str | None, extra_seconds: int = 300):
    """Extend the TTL on the GPU lock (for long-running tasks)."""
    if token is None:
        return
    r = _redis()
    if r is None:
        return
    current = r.get(GPU_LOCK_KEY)
    if current == token:
        r.expire(GPU_LOCK_KEY, GPU_LOCK_TTL + extra_seconds)


class GpuLock:
    """
    Context manager for GPU mutex acquisition.

    Usage:
        with GpuLock("ai engineer", timeout=300):
            result = run_ollama(...)
    """

    def __init__(self, agent_name: str, timeout: int = DEFAULT_TIMEOUT):
        self.agent_name = agent_name
        self.timeout = timeout
        self._token: str | None = None

    def __enter__(self):
        self._token = acquire_gpu(self.agent_name, self.timeout)
        return self

    def __exit__(self, *_):
        release_gpu(self._token)
        self._token = None


def gpu_status() -> dict:
    """Return current GPU lock status (for monitoring/health checks)."""
    r = _redis()
    if r is None:
        return {"available": True, "holder": None, "redis": False}
    holder = r.get(GPU_LOCK_KEY)
    ttl = r.ttl(GPU_LOCK_KEY)
    return {
        "available": holder is None,
        "holder": holder,
        "ttl_seconds": ttl,
        "redis": True,
    }
