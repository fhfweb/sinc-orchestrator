"""
streaming/core/circuit.py
=========================
Simple async circuit breaker implementation to prevent cascading failures.
"""

import asyncio
import logging
import time
import json
from enum import Enum
from functools import wraps
from .redis_ import get_async_redis

log = logging.getLogger("orchestrator.circuit")

class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

class CircuitBreaker:
    def __init__(self, name: str, threshold: int = 5, recovery_timeout: int = 30):
        self.name = name
        self.threshold = threshold
        self.recovery_timeout = recovery_timeout
        # Keys for Redis
        self.state_key = f"cb:state:{name}"
        self.fail_key = f"cb:fail:{name}"
        # Fallback for memory-only if Redis is down
        self._mem_state = CircuitState.CLOSED
        self._mem_failures = 0
        self._mem_last_fail = 0

    async def _get_state(self, r) -> CircuitState:
        if not r: return self._mem_state
        try:
            val = await r.get(self.state_key)
            if not val: return CircuitState.CLOSED
            if isinstance(val, bytes): val = val.decode("utf-8")
            if val == "open": return CircuitState.OPEN
            if val == "half_open": return CircuitState.HALF_OPEN
            return CircuitState.CLOSED
        except Exception:
            return self._mem_state

    async def call(self, func, *args, **kwargs):
        r = get_async_redis()
        state = await self._get_state(r)

        if state == CircuitState.OPEN:
            if r:
                ttl = await r.ttl(self.state_key)
                if ttl <= 0:
                    state = CircuitState.HALF_OPEN
                    await r.set(self.state_key, "half_open")
                else:
                    raise Exception(f"Circuit {self.name} is OPEN (fail-fast, TTL={ttl}s)")
            else:
                if time.time() - self._mem_last_fail > self.recovery_timeout:
                    state = CircuitState.HALF_OPEN
                else:
                    raise Exception(f"Circuit {self.name} is OPEN (fail-fast, mem)")

        try:
            start_t = time.time()
            result = await func(*args, **kwargs)
            duration = (time.time() - start_t) * 1000
            
            if state in (CircuitState.HALF_OPEN, CircuitState.OPEN):
                log.info(f"Circuit {self.name} recovered -> CLOSED")
                if r:
                    await r.delete(self.state_key)
                    await r.delete(self.fail_key)
                    # Log recovery event in Redis for global tracking
                    await r.lpush(f"cb:history:{self.name}", json.dumps({
                        "event": "recovered", "ts": time.time(), "latency": duration
                    }))
                    await r.ltrim(f"cb:history:{self.name}", 0, 99)
                self._mem_state = CircuitState.CLOSED
                self._mem_failures = 0
            return result
        except Exception as e:
            if r:
                fails = await r.incr(self.fail_key)
                await r.expire(self.fail_key, 60)
                if fails >= self.threshold:
                    log.warning(f"Circuit {self.name} opening (Redis failures={fails})")
                    await r.setex(self.state_key, self.recovery_timeout, "open")
                    # Log failure in Redis
                    await r.lpush(f"cb:history:{self.name}", json.dumps({
                        "event": "opened", "ts": time.time(), "error": str(e)
                    }))
                    await r.ltrim(f"cb:history:{self.name}", 0, 99)
            else:
                self._mem_failures += 1
                self._mem_last_fail = time.time()
                if self._mem_failures >= self.threshold:
                    self._mem_state = CircuitState.OPEN
            raise e

_breakers = {}

def get_breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name)
    return _breakers[name]

def circuit_breaker(name: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            breaker = get_breaker(name)
            return await breaker.call(func, *args, **kwargs)
        return wrapper
    return decorator
