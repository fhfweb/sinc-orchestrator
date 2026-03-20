"""
streaming/core/circuit.py
=========================
Simple async circuit breaker implementation to prevent cascading failures.
"""

import asyncio
import logging
import time
from enum import Enum
from functools import wraps

log = logging.getLogger("orchestrator.circuit")

class CircuitState(Enum):
    CLOSED = "closed"      # System is normal, requests flow
    OPEN = "open"          # System is failing, requests blocked
    HALF_OPEN = "half_open" # System is testing recovery

class CircuitBreaker:
    def __init__(self, name: str, threshold: int = 5, recovery_timeout: int = 30):
        self.name = name
        self.threshold = threshold
        self.recovery_timeout = recovery_timeout
        self.failure_count = 0
        self.state = CircuitState.CLOSED
        self.last_failure_time = 0

    async def call(self, func, *args, **kwargs):
        if self.state == CircuitState.OPEN:
            if time.time() - self.last_failure_time > self.recovery_timeout:
                log.info(f"Circuit {self.name} switching to HALF-OPEN")
                self.state = CircuitState.HALF_OPEN
            else:
                raise Exception(f"Circuit {self.name} is OPEN (fail-fast)")

        try:
            result = await func(*args, **kwargs)
            if self.state == CircuitState.HALF_OPEN:
                log.info(f"Circuit {self.name} switching to CLOSED (Recovered)")
                self.state = CircuitState.CLOSED
                self.failure_count = 0
            return result
        except Exception as e:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.failure_count >= self.threshold:
                log.warning(f"Circuit {self.name} switching to OPEN (failure_count={self.failure_count})")
                self.state = CircuitState.OPEN
            raise e

# Global registry of circuit breakers
_breakers = {}

def get_breaker(name: str) -> CircuitBreaker:
    if name not in _breakers:
        _breakers[name] = CircuitBreaker(name)
    return _breakers[name]

def circuit_breaker(name: str):
    """Decorator to apply circuit breaker to an async function."""
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            breaker = get_breaker(name)
            return await breaker.call(func, *args, **kwargs)
        return wrapper
    return decorator
