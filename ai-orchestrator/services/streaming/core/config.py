"""
streaming/core/config.py
========================
Infrastructure configuration: DB, Redis, HTTP, filesystem paths, and timeouts.

This module is the canonical configuration layer for the Python control plane.
It supports both:
  - container execution (compose services talking to `orchestrator-task-db:5432`)
  - host execution (developer shells and smoke tests using the published port)

The resolution order is:
  1. explicit environment variables
  2. ai-orchestrator/docker/.env
  3. ai-orchestrator/docker/.env.docker.generated
  4. hard defaults
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable


BASE = Path(__file__).resolve().parents[3]  # ai-orchestrator/
REPO_ROOT = BASE.parent

TASKS_DAG = BASE / "tasks" / "task-dag.json"
STREAM_EVENTS = BASE / "state" / "stream-events.jsonl"
HITL_GATES = BASE / "state" / "hitl-gates.json"
WHITEBOARD = BASE / "state" / "whiteboard.json"
HEALTH_REPORT = BASE / "state" / "health-report.json"
COMPLETIONS = BASE / "tasks" / "completions"
DISPATCHES = BASE / "state" / "external-agent-bridge" / "dispatches"
AGENTS_WORKLOAD = BASE / "agents" / "workload.json"
AGENTS_REPUTATION = BASE / "agents" / "reputation.json"
LOOP_STATE = BASE / "state" / "loop-state.json"
POLICY_REPORT = BASE / "reports" / "latest-policy-report.json"

_ENV_FILES = (
    BASE / "docker" / ".env",
    BASE / "docker" / ".env.docker.generated",
    BASE / "docker" / ".env.example",
)
_ENV_FILE_VALUES: dict[str, str] = {}


def _load_env_file_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in _ENV_FILES:
        if not path.exists():
            continue
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("'").strip('"')
                if key and key not in values:
                    values[key] = value
        except Exception:
            continue
    return values


_ENV_FILE_VALUES = _load_env_file_values()


def env_get(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.environ.get(name)
        if value not in (None, ""):
            return value
    for name in names:
        value = _ENV_FILE_VALUES.get(name)
        if value not in (None, ""):
            return value
    return default


def env_get_int(*names: str, default: int) -> int:
    value = env_get(*names)
    if value in (None, ""):
        return default
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def env_get_float(*names: str, default: float) -> float:
    value = env_get(*names)
    if value in (None, ""):
        return default
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _bool_env(value: str | None, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def env_get_bool(*names: str, default: bool = False) -> bool:
    return _bool_env(env_get(*names), default=default)


def _running_in_container() -> bool:
    if env_get("ORCHESTRATOR_RUNTIME_ENV", default="").strip().lower() == "docker":
        return True
    if env_get("DOTNET_RUNNING_IN_CONTAINER", default="").strip() == "true":
        return True
    if env_get("KUBERNETES_SERVICE_HOST"):
        return True
    return Path("/.dockerenv").exists()


def _resolve_db_host_port() -> tuple[str, int]:
    raw_host = env_get("ORCHESTRATOR_TASK_DB_HOST", "ORCH_DB_HOST", default="orchestrator-task-db") or "orchestrator-task-db"
    raw_port = env_get_int("ORCHESTRATOR_TASK_DB_PORT", "ORCH_DB_PORT", default=5432)
    fallback_host = env_get(
        "ORCHESTRATOR_TASK_DB_HOST_FALLBACK",
        "ORCH_DB_HOST_FALLBACK",
        default="127.0.0.1",
    ) or "127.0.0.1"
    fallback_port = env_get_int(
        "ORCHESTRATOR_TASK_DB_PORT_FALLBACK",
        "ORCH_DB_PORT_FALLBACK",
        default=5434,
    )
    docker_aliases = {
        "orchestrator-task-db",
        "orchestrator-db",
        "postgres",
        "postgresql",
        "db",
    }
    if not _running_in_container() and raw_host.strip().lower() in docker_aliases:
        return fallback_host, fallback_port
    return raw_host, raw_port

def _resolve_redis_host_port() -> tuple[str, int]:
    raw_host = env_get("REDIS_HOST", default="localhost") or "localhost"
    raw_port = env_get_int("REDIS_PORT", default=6379)
    docker_aliases = {"redis", "server", "cache", "broker"}
    if not _running_in_container() and raw_host.strip().lower() in docker_aliases:
        return "127.0.0.1", raw_port
    return raw_host, raw_port


def _resolve_qdrant_host_port() -> tuple[str, int]:
    raw_host = env_get("QDRANT_HOST", default="qdrant") or "qdrant"
    raw_port = env_get_int("QDRANT_PORT", default=6333)
    docker_aliases = {"qdrant", "vector-db", "memory"}
    if not _running_in_container() and raw_host.strip().lower() in docker_aliases:
        return "127.0.0.1", raw_port
    return raw_host, raw_port


def _resolve_ollama_url() -> str:
    raw_url = env_get("OLLAMA_HOST", default="http://ollama:11434") or "http://ollama:11434"
    if not _running_in_container() and "ollama" in raw_url:
        return raw_url.replace("ollama", "127.0.0.1")
    return raw_url.rstrip("/")



DB_HOST, DB_PORT = _resolve_db_host_port()
DB_CONFIG = {
    "dbname": env_get("ORCHESTRATOR_TASK_DB_NAME", "ORCH_DB_NAME", default="orchestrator_tasks"),
    "user": env_get("ORCHESTRATOR_TASK_DB_USER", "ORCH_DB_USER", default="orchestrator"),
    "password": env_get("ORCHESTRATOR_TASK_DB_PASSWORD", "ORCH_DB_PASSWORD", default=""),
    "host": DB_HOST,
    "port": DB_PORT,
    "sslmode": env_get("ORCHESTRATOR_TASK_DB_SSLMODE", "ORCH_DB_SSLMODE", default="disable"),
    "connect_timeout": env_get_int(
        "ORCHESTRATOR_TASK_DB_CONNECT_TIMEOUT_SECONDS",
        "ORCH_DB_CONNECT_TIMEOUT_SECONDS",
        default=5,
    ),
}
DB_CONFIG_SOURCE = {
    "host_source": "fallback" if DB_HOST != (env_get("ORCHESTRATOR_TASK_DB_HOST", "ORCH_DB_HOST", default="orchestrator-task-db") or "orchestrator-task-db") else "direct",
    "env_files": [str(path) for path in _ENV_FILES if path.exists()],
}
DB_POOL_MAX = env_get_int("DB_POOL_MAX", default=10)
SLOW_QUERY_MS = env_get_int("SLOW_QUERY_MS", default=200)

PORT = env_get_int("PORT", default=8765)
MAX_REQUEST_BYTES = env_get_int("MAX_REQUEST_BYTES", default=2 * 1024 * 1024)
CORS_ORIGINS = env_get("CORS_ORIGINS", default="*") or "*"

REDIS_HOST, REDIS_PORT = _resolve_redis_host_port()
REDIS_DB = env_get_int("REDIS_CACHE_DB", default=2)
REDIS_PASSWORD = env_get("REDIS_PASSWORD", default=None) or None


ORCHESTRATOR_API_KEY = env_get("ORCHESTRATOR_API_KEY", default="") or ""

TASK_MAX_RETRIES = env_get_int("TASK_MAX_RETRIES", default=3)
TASK_STALE_TIMEOUT_M = env_get_int("TASK_STALE_TIMEOUT_M", default=2)
TASK_RETRY_BACKOFF_S = [60, 300, 1800]

QDRANT_HOST, QDRANT_PORT = _resolve_qdrant_host_port()
OLLAMA_HOST = _resolve_ollama_url()

ASK_CACHE_TTL = env_get_int("ASK_CACHE_TTL", default=300)
MEMORY_L2_TIMEOUT_S = env_get_float("MEMORY_L2_TIMEOUT_S", default=3.0)
MEMORY_L3_TIMEOUT_S = env_get_float("MEMORY_L3_TIMEOUT_S", default=2.0)
RATE_LIMIT_FAIL_OPEN = env_get_bool("RATE_LIMIT_FAIL_OPEN", default=False)

# ── Neo4j ──────────────────────────────────────────────────────────────────────
NEO4J_URI  = env_get("NEO4J_URI",  default="bolt://neo4j:7687")
NEO4J_USER = env_get("NEO4J_USER", default="neo4j")
NEO4J_PASS = (env_get("NEO4J_PASS") or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1])

# ── Ingest pipeline ────────────────────────────────────────────────────────────
CHUNK_SIZE     = env_get_int("INGEST_CHUNK_SIZE",    default=1500)
CHUNK_OVERLAP  = env_get_int("INGEST_CHUNK_OVERLAP", default=150)
DISPATCHES_DIR = env_get("DISPATCHES_DIR", default=str(BASE / "state" / "external-agent-bridge" / "dispatches"))
