"""
streaming/core/billing.py
=========================
Plan definitions, tier limits, and SSE connection quotas.
Separated from config.py so billing rules can evolve independently
of infrastructure settings.
"""

# ── SSE connection limits per plan ────────────────────────────────────────────
SSE_DEFAULT_LIMIT = 10
SSE_LIMITS: dict[str, int] = {
    "free":       2,
    "pro":        20,
    "enterprise": 100,
}

# ── Plan feature gates ────────────────────────────────────────────────────────
PLAN_FEATURES: dict[str, dict] = {
    "free": {
        "backends":     ["ollama"],
        "max_tokens":   4096,
        "max_tasks":    100,
        "max_projects": 3,
    },
    "pro": {
        "backends":     ["ollama", "anthropic"],
        "max_tokens":   32768,
        "max_tasks":    10000,
        "max_projects": 50,
    },
    "enterprise": {
        "backends":     ["ollama", "anthropic"],
        "max_tokens":   200000,
        "max_tasks":    -1,
        "max_projects": -1,
    },
}


def allowed_backends(plan: str) -> list[str]:
    return PLAN_FEATURES.get(plan, PLAN_FEATURES["free"])["backends"]


def check_backend_access(plan: str, backend: str) -> bool:
    return backend in allowed_backends(plan)
