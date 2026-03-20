"""
DEPRECATED LEGACY MODULE
========================
`services/orchestrator_core.py` is no longer part of the official runtime
 topology. The canonical control plane now lives under `services/streaming`
 and is served exclusively from port `8765`.

This module remains in the repository only for migration audit, rollback
analysis, and controlled parity extraction. Do not add new runtime behavior
here and do not reintroduce it into the official compose stack.
"""
"""
Orchestrator Core â€” Python Brain  v4
=====================================
Central coordination service for the SINC Orchestrator Platform.

New in v4
---------
  FastAPI           â€” async-first API (replaces Flask); native SSE + WebSocket support
  uvicorn           â€” ASGI server (replaces Flask dev server)
  OpenTelemetry     â€” distributed tracing: taskâ†’agentâ†’LLMâ†’commit spans
  lifespan ctx mgr  â€” clean startup/shutdown (scheduler, pool, OTEL)

Existing libraries (v3)
-----------------------
  structlog          â€” structured JSON logging
  psycopg-pool       â€” PostgreSQL connection pool
  prometheus-client  â€” Prometheus metrics exposition
  APScheduler        â€” background scheduler + watchdog
  networkx           â€” in-memory DAG analysis
  tenacity           â€” declarative retry with exponential backoff
  redis              â€” task priority queue + distributed scheduler lock
  pydantic v2        â€” schema validation for incoming API bodies

Port: 8767  (streaming_server=8765, dashboard=8766, core=8767)

Environment variables
---------------------
ORCH_DB_NAME / ORCH_DB_USER / ORCH_DB_PASSWORD / ORCH_DB_HOST / ORCH_DB_PORT
REDIS_HOST / REDIS_PORT / REDIS_PASSWORD / REDIS_DB
ORCHESTRATOR_API_KEY        â€” shared API key (same as ADMIN_API_KEY in streaming_server)
CORE_API_HOST / CORE_API_PORT
SCHEDULER_INTERVAL_S        â€” scheduler tick interval (default 30)
WATCHDOG_INTERVAL_S         â€” stale claim sweep interval (default 60)
STALE_HEARTBEAT_THRESHOLD_S â€” seconds without heartbeat before task is recycled (default 120)
WORKSPACE_BACKEND           â€” local | s3 | github  (default local)
WORKSPACE_ROOT              â€” workspace root path or S3 URI or owner/repo@ref
MAX_AGENT_ACTIVE_TASKS      â€” max concurrent tasks per agent (default 2)
BACKOFF_BASE_S              â€” base backoff seconds (default 30)
BACKOFF_MAX_S               â€” max backoff seconds (default 3600)
DB_POOL_MIN / DB_POOL_MAX   â€” psycopg-pool size (default 2 / 10)
OTEL_EXPORTER_OTLP_ENDPOINT â€” OTLP gRPC endpoint (optional; console exporter used if absent)
OTEL_SERVICE_NAME           â€” service name in traces (default orchestrator-core)
"""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import re as _re
import sys
import time
import threading
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Generator, Optional

# â”€â”€ structlog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import structlog

structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.stdlib.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(sys.stdout),
)
log = structlog.get_logger("orchestrator_core")

# â”€â”€ psycopg + psycopg-pool â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import psycopg
import psycopg.rows
from psycopg_pool import ConnectionPool

# â”€â”€ pydantic v2 â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from pydantic import BaseModel, Field, field_validator

# â”€â”€ prometheus-client â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from prometheus_client import (
    Counter, Gauge, Histogram,
    generate_latest, CONTENT_TYPE_LATEST,
    REGISTRY,
)

# â”€â”€ APScheduler â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.executors.pool import ThreadPoolExecutor as APThreadPool

# â”€â”€ networkx â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
import networkx as nx

# â”€â”€ tenacity â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from tenacity import (
    retry, stop_after_attempt, wait_exponential,
    retry_if_exception_type, before_sleep_log,
)
import logging as _stdlib_logging

# â”€â”€ FastAPI + uvicorn â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import uvicorn

# â”€â”€ OpenTelemetry â€” shared setup module â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
from services.otel_setup import configure_otel, span as _otel_span, instrument_fastapi, instrument_psycopg, instrument_redis
from services.streaming.core.config import DB_CONFIG as STREAMING_DB_CONFIG

# â”€â”€ Event Store â€” append-only event log (optional import) â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
try:
    from services.event_store import (
        emit_task_claimed   as _es_task_claimed,
        emit_task_created   as _es_task_created,
        emit_task_completed as _es_task_completed,
        emit_task_failed    as _es_task_failed,
        refresh_projections as _es_refresh_projections,
    )
    _HAS_EVENT_STORE = True
except ImportError:
    _HAS_EVENT_STORE = False


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# CONFIGURATION
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_DB_DSN = (
    f"dbname={STREAMING_DB_CONFIG['dbname']} "
    f"user={STREAMING_DB_CONFIG['user']} "
    f"password={STREAMING_DB_CONFIG['password']} "
    f"host={STREAMING_DB_CONFIG['host']} "
    f"port={STREAMING_DB_CONFIG['port']}"
)

REDIS_HOST     = os.environ.get("REDIS_HOST",     "localhost")
REDIS_PORT     = int(os.environ.get("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "") or None
REDIS_DB       = int(os.environ.get("REDIS_DB",   "1"))

API_KEY    = os.environ.get("ORCHESTRATOR_API_KEY", "")
CORE_HOST  = os.environ.get("CORE_API_HOST", "0.0.0.0")
CORE_PORT  = int(os.environ.get("CORE_API_PORT", "8767"))

SCHEDULER_INTERVAL_S        = int(os.environ.get("SCHEDULER_INTERVAL_S",        "30"))
WATCHDOG_INTERVAL_S         = int(os.environ.get("WATCHDOG_INTERVAL_S",         "60"))
STALE_HEARTBEAT_THRESHOLD_S = int(os.environ.get("STALE_HEARTBEAT_THRESHOLD_S", "120"))
MAX_AGENT_ACTIVE_TASKS      = int(os.environ.get("MAX_AGENT_ACTIVE_TASKS",      "2"))
BACKOFF_BASE_S              = float(os.environ.get("BACKOFF_BASE_S",            "30"))
BACKOFF_MAX_S               = float(os.environ.get("BACKOFF_MAX_S",             "3600"))
MIN_SAMPLES_VALID           = int(os.environ.get("MIN_SAMPLES_VALID",           "100"))
DB_POOL_MIN                 = int(os.environ.get("DB_POOL_MIN",                 "2"))
DB_POOL_MAX                 = int(os.environ.get("DB_POOL_MAX",                 "10"))

QUEUE_KEY            = "orch:queue"
QUEUE_CLAIMED_PREFIX = "orch:claimed:"
SCHEDULER_LOCK_KEY   = "orch:scheduler:lock"

OTEL_SERVICE_NAME    = os.environ.get("OTEL_SERVICE_NAME", "orchestrator-core")
OTEL_OTLP_ENDPOINT   = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# OPENTELEMETRY â€” delegate to shared otel_setup module
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

# _otel_span is the context-manager from services.otel_setup; alias as _span for brevity
_span = _otel_span


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DATABASE â€” psycopg-pool
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = ConnectionPool(
                conninfo=_DB_DSN,
                min_size=DB_POOL_MIN,
                max_size=DB_POOL_MAX,
                kwargs={"row_factory": psycopg.rows.dict_row},
                open=True,
                reconnect_timeout=30,
            )
            log.info("db_pool_opened", min=DB_POOL_MIN, max=DB_POOL_MAX)
    return _pool


@contextmanager
def _db() -> Generator[psycopg.Connection, None, None]:
    """Lease a connection from the pool; commit on success, rollback on error."""
    with _get_pool().connection() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


def _table_columns(table_name: str) -> set[str]:
    with _db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name
                  FROM information_schema.columns
                 WHERE table_schema = 'public'
                   AND table_name = %s
                """,
                (table_name,),
            )
            return {row["column_name"] for row in cur.fetchall()}


def _task_pk_column() -> str:
    return "task_id" if "task_id" in _table_columns("tasks") else "id"


def _dependency_ref_column() -> str:
    return "dependency_id" if "dependency_id" in _table_columns("dependencies") else "depends_on"


def _heartbeat_time_column() -> str:
    return "beat_at" if "beat_at" in _table_columns("heartbeats") else "updated_at"


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=5),
    retry=retry_if_exception_type(psycopg.OperationalError),
    before_sleep=before_sleep_log(_stdlib_logging.getLogger("tenacity.db"), _stdlib_logging.WARNING),
)
def _db_ok() -> bool:
    with _db() as c:
        with c.cursor() as cur:
            cur.execute("SELECT 1")
    return True


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# REDIS â€” lazy singleton with reconnect on failure
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_rc: Any = None
_rc_lck  = threading.Lock()


def _redis_client():
    global _rc
    with _rc_lck:
        if _rc is not None:
            return _rc
        try:
            import redis as _rl
            r = _rl.Redis(
                host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB,
                password=REDIS_PASSWORD, decode_responses=True,
                socket_timeout=3, socket_connect_timeout=3,
                retry_on_timeout=True,
            )
            r.ping()
            _rc = r
            log.info("redis_connected", host=REDIS_HOST, port=REDIS_PORT, db=REDIS_DB)
        except Exception as e:
            log.warning("redis_unavailable", error=str(e), fallback="postgresql")
            _rc = None
        return _rc


def _r():
    r = _redis_client()
    if r is None:
        return None
    try:
        r.ping()
        return r
    except Exception:
        with _rc_lck:
            globals()["_rc"] = None
        return None


def _redis_ok() -> bool:
    try:
        r = _r()
        return bool(r and r.ping())
    except Exception:
        return False


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# PROMETHEUS METRICS
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _get_metric(cls, name, documentation, labelnames=()):
    """Idempotent metric registration to prevent ValueError upon re-import or uvicorn worker startup."""
    if name in REGISTRY._names_to_collectors:
        return REGISTRY._names_to_collectors[name]
    return cls(name, documentation, labelnames)

_m_tasks        = _get_metric(Gauge,     "orch_tasks",                  "Task count by status", ["status"])
_m_queue_depth  = _get_metric(Gauge,     "orch_queue_depth",            "Redis queue depth")
_m_agents       = _get_metric(Gauge,     "orch_agents_total",           "Registered agents")
_m_tick_total   = _get_metric(Counter,   "orch_scheduler_ticks_total",  "Scheduler ticks executed")
_m_promoted     = _get_metric(Counter,   "orch_tasks_promoted_total",   "Tasks promoted waitingâ†’pending")
_m_dispatched   = _get_metric(Counter,   "orch_tasks_dispatched_total", "Tasks pushed to queue")
_m_recycled     = _get_metric(Counter,   "orch_tasks_recycled_total",   "Stale tasks recycled by watchdog")
_m_seeded       = _get_metric(Counter,   "orch_tasks_seeded_total",     "Tasks auto-seeded from entropy")
_m_tick_latency = _get_metric(Histogram, "orch_scheduler_tick_seconds", "Scheduler tick duration")
_m_db_pool_size = _get_metric(Gauge,     "orch_db_pool_size",           "DB pool connections", ["state"])
_m_http_reqs    = _get_metric(Counter,   "orch_http_requests_total",    "HTTP requests", ["method", "path", "status"])


def _refresh_metrics():
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT status, COUNT(*) n FROM tasks GROUP BY status")
                for row in cur.fetchall():
                    _m_tasks.labels(status=row["status"]).set(row["n"])
                cur.execute("SELECT COUNT(*) n FROM agent_reputation")
                _m_agents.set((cur.fetchone() or {}).get("n", 0))
    except Exception:
        pass
    _m_queue_depth.set(queue_depth())
    try:
        pool  = _get_pool()
        stats = pool.get_stats()
        _m_db_pool_size.labels(state="available").set(stats.get("pool_available", 0))
        _m_db_pool_size.labels(state="busy").set(
            stats.get("pool_size", 0) - stats.get("pool_available", 0))
    except Exception:
        pass


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# PYDANTIC v2 â€” REQUEST BODY SCHEMAS
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

class EnqueueBody(BaseModel):
    task_id:       str
    urgency:       str  = "medium"
    priority:      str  = "P2"
    critical_path: bool = False
    entropy_score: float = Field(0.0, ge=0.0, le=1.0)


class DequeueBody(BaseModel):
    agent_name:  str = "unknown"
    claim_ttl_s: int = Field(120, ge=10, le=3600)


class SeedEntropyBody(BaseModel):
    project_id: str
    threshold:  float = Field(0.75, ge=0.0, le=1.0)


class WorkspaceWriteBody(BaseModel):
    path:    str
    content: str

    @field_validator("path")
    @classmethod
    def no_traversal(cls, v: str) -> str:
        if ".." in v:
            raise ValueError("path traversal not allowed")
        return v


class ScoreTaskBody(BaseModel):
    title:       str = ""
    description: str = ""


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# AUTH + REQUEST CONTEXT â€” FastAPI dependencies
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _auth_only(x_api_key: str = Header(default="", alias="X-Api-Key")) -> None:
    """Enforces API key; used via dependencies=[Depends(_auth_only)] on routes."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="unauthorized")


_ID_RE = _re.compile(r'^[a-zA-Z0-9_\-.]{1,128}$')


def _validate_id(value: str, field: str) -> str:
    """Reject values that could be used for path traversal or injection."""
    if not _ID_RE.match(value):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {value!r}")
    return value


def _get_tenant(
    x_tenant_id: str = Header(default="local", alias="X-Tenant-ID"),
) -> str:
    """Extracts and validates tenant_id header for multi-tenant queries."""
    return _validate_id(x_tenant_id, "tenant_id")


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# WILSON CONFIDENCE INTERVAL
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 1.0
    p, z2, n = successes / total, z * z, total
    mid  = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / (1 + z2 / n)
    return max(0.0, mid - half), min(1.0, mid + half)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DOMAIN AFFINITY KEYWORDS
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_DOMAIN_KEYWORDS = [
    (_re.compile(r"\b(api|rest|endpoint|route|controller|laravel|php|backend|service|model|migration)\b", _re.I), "backend_affinity"),
    (_re.compile(r"\b(react|vue|angular|css|html|ui|ux|frontend|component|blade|tailwind)\b",             _re.I), "frontend_affinity"),
    (_re.compile(r"\b(sql|query|postgres|mysql|database|schema|index|migration|eloquent|db)\b",            _re.I), "db_affinity"),
    (_re.compile(r"\b(architect|design|pattern|refactor|adr|ddd|bounded|domain|hexagonal|clean)\b",       _re.I), "arch_affinity"),
    (_re.compile(r"\b(test|spec|qa|coverage|phpunit|assertion|mock|fixture|tdd|pester)\b",                _re.I), "qa_affinity"),
    (_re.compile(r"\b(docker|compose|ci|cd|deploy|kubernetes|k8s|pipeline|github.action|infra|devops)\b", _re.I), "devops_affinity"),
]

_SAFE_AFFINITY_COLS = frozenset({
    "backend_affinity", "frontend_affinity", "db_affinity",
    "arch_affinity", "qa_affinity", "devops_affinity", "reputation_fit_score",
})


def _infer_domain(title: str, description: str = "") -> str:
    text = f"{title} {description}"
    for pattern, col in _DOMAIN_KEYWORDS:
        if pattern.search(text):
            return col
    return "reputation_fit_score"


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# PRIORITY SCORE
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_URGENCY_W = {"P0": 1000, "critical": 900, "P1": 500, "high": 400,
               "P2": 200, "medium": 150, "low": 50}


def _priority_score(urgency: str, critical_path: bool = False,
                    entropy: float = 0.0, created_ts: float = 0.0) -> float:
    base     = _URGENCY_W.get(urgency, 150)
    cp_bonus = 200 if critical_path else 0
    age_h    = (time.time() - created_ts) / 3600 if created_ts else 0.0
    return base + cp_bonus + entropy * 10 + min(age_h * 0.5, 30)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# BACKOFF
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def _compute_backoff_until(conflict_count: int) -> datetime:
    delay = min(BACKOFF_BASE_S * (2 ** conflict_count), BACKOFF_MAX_S)
    return datetime.now(timezone.utc) + timedelta(seconds=delay)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# DAG ENGINE â€” uses `dependencies` table + networkx for graph analysis
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def get_ready_tasks(tenant_id: str = "local", limit: int = 100) -> list[dict]:
    """Return tasks in (pending, waiting) whose all dependencies are terminal.
    Optimized: priorities tasks that unlock more dependents (out-degree).
    """
    with _span("dag.get_ready_tasks"):
        try:
            with _db() as conn:
                with conn.cursor() as cur:
                    # 1. Fetch candidates (currently actionable)
                    cur.execute("""
                        SELECT t.id, t.title, t.description,
                               t.urgency, t.priority, t.status,
                               t.critical_path, t.project_id, t.tenant_id,
                               t.assigned_agent, t.plan_id,
                               COALESCE(t.entropy_score, 0)::float  AS entropy_score,
                               COALESCE(t.lock_backoff_until, NOW() - INTERVAL '1s') AS backoff_until,
                               EXTRACT(EPOCH FROM t.created_at)::float AS created_ts
                        FROM tasks t
                        WHERE t.tenant_id = %s
                          AND t.status IN ('pending', 'waiting')
                          AND COALESCE(t.lock_backoff_until, NOW() - INTERVAL '1s') <= NOW()
                        LIMIT %s
                    """, (tenant_id, limit * 10))  # Fetch more to allow for filtering
                    candidates = cur.fetchall()
                    if not candidates:
                        return []

                    # 2. Fetch terminal task IDs
                    cur.execute("""
                        SELECT id FROM tasks WHERE tenant_id = %s
                          AND status IN ('done','skipped','failed','cancelled','dead-letter')
                    """, (tenant_id,))
                    terminal = {r["id"] for r in cur.fetchall()}

                    # 3. Fetch dependencies for ready check
                    cand_ids = [t["id"] for t in candidates]
                    cur.execute("""
                        SELECT task_id, dependency_id FROM dependencies
                        WHERE task_id = ANY(%s)
                    """, (cand_ids,))
                    deps_map: dict[str, list[str]] = {}
                    for row in cur.fetchall():
                        deps_map.setdefault(row["task_id"], []).append(row["dependency_id"])

            # 4. Filter for ready tasks
            ready_pool = []
            for task in candidates:
                tid = task["id"]
                if all(d in terminal for d in deps_map.get(tid, [])):
                    ready_pool.append(task)
            
            if not ready_pool:
                return []

            # 5. Build Graph for Out-Degree Optimization (Deterministic Tie-Breaking)
            # We only need to know how many non-terminal tasks depend on our ready tasks.
            # Directly querying the DB for out-degree might be faster than building a full graph.
            ready_ids = [t["id"] for t in ready_pool]
            try:
                task_pk = _task_pk_column()
                dep_ref_col = _dependency_ref_column()
                with _db() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            SELECT d.{dep_ref_col} AS dependency_id, COUNT(*) AS out_degree
                            FROM dependencies d
                            JOIN tasks t ON t.{task_pk} = d.task_id
                            WHERE d.{dep_ref_col} = ANY(%s)
                              AND t.status NOT IN ('done','skipped','failed','cancelled','dead-letter')
                            GROUP BY d.{dep_ref_col}
                        """.format(dep_ref_col=dep_ref_col, task_pk=task_pk), (ready_ids,))
                        out_degrees = {r["dependency_id"]: r["out_degree"] for r in cur.fetchall()}
            except Exception:
                out_degrees = {}

            # 6. Final Deterministic Sort
            # Tie-breakers: Priority > Out-degree > Critical Path > Created At > ID
            def sort_key(t):
                p_map = {"P0": 0, "critical": 0, "P1": 1, "high": 1, "P2": 2, "medium": 3, "low": 4}
                p_val = p_map.get(t["priority"], 9)
                if p_val == 9: # check urgency fallback
                    p_val = p_map.get(t["urgency"], 9)
                
                # We want: 
                # - Low p_val first
                # - High out_degree first (-val)
                # - True critical_path first (not val)
                # - Low created_ts first
                # - Alphabetical ID last
                return (
                    p_val,
                    -out_degrees.get(t["id"], 0),
                    not t["critical_path"],
                    t["created_ts"],
                    t["id"]
                )

            ready_pool.sort(key=sort_key)
            return [dict(t) for t in ready_pool[:limit]]
        except Exception as e:
            log.error("get_ready_tasks_error", error=str(e))
            return []


def get_blocked_tasks(tenant_id: str = "local") -> list[dict]:
    try:
        task_pk = _task_pk_column()
        dep_ref_col = _dependency_ref_column()
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT t.{task_pk} AS id, t.title, t.status,
                           ARRAY_AGG(d.{dep_ref_col}) AS blocking_deps,
                           COUNT(d.{dep_ref_col}) AS block_count
                    FROM tasks t
                    JOIN dependencies d ON d.task_id = t.{task_pk}
                    JOIN tasks dep ON dep.{task_pk} = d.{dep_ref_col}
                                  AND dep.status NOT IN
                                      ('done','skipped','failed','cancelled','dead-letter')
                    WHERE t.tenant_id = %s
                      AND t.status IN ('pending','waiting','blocked-deps')
                    GROUP BY t.{task_pk}, t.title, t.status
                    ORDER BY block_count DESC LIMIT 100
                """.format(task_pk=task_pk, dep_ref_col=dep_ref_col), (tenant_id,))
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error("get_blocked_tasks_error", error=str(e))
        return []


def dag_graph(tenant_id: str = "local", project_id: str = "") -> dict:
    """Return full DAG as {nodes, edges} plus networkx-computed analytics."""
    with _span("dag.build_graph"):
        try:
            task_pk = _task_pk_column()
            dep_ref_col = _dependency_ref_column()
            with _db() as conn:
                with conn.cursor() as cur:
                    where  = "tenant_id = %s" + (" AND project_id = %s" if project_id else "")
                    params: list = [tenant_id] + ([project_id] if project_id else [])
                    cur.execute(f"""
                        SELECT {task_pk} AS id, title, status, urgency, priority, critical_path, project_id
                        FROM tasks WHERE {where} ORDER BY created_at
                    """, params)
                    tasks = {r["id"]: dict(r) for r in cur.fetchall()}

                    cur.execute("""
                        SELECT task_id, {dep_ref_col} AS dependency_id FROM dependencies
                        WHERE task_id = ANY(%s)
                    """.format(dep_ref_col=dep_ref_col), (list(tasks.keys()),))
                    edges = [{"from": r["dependency_id"], "to": r["task_id"]}
                             for r in cur.fetchall()]

            G = nx.DiGraph()
            G.add_nodes_from(tasks.keys())
            G.add_edges_from([(e["from"], e["to"]) for e in edges
                              if e["from"] in tasks and e["to"] in tasks])

            critical_path: list[str] = []
            longest_path_length      = 0
            try:
                if nx.is_directed_acyclic_graph(G):
                    critical_path       = nx.dag_longest_path(G)
                    longest_path_length = nx.dag_longest_path_length(G)
            except Exception:
                pass

            bottlenecks = sorted(
                [{"task_id": n, "out_degree": G.out_degree(n), "in_degree": G.in_degree(n)}
                 for n in G.nodes],
                key=lambda x: x["out_degree"], reverse=True,
            )[:5]

            for tid, node in tasks.items():
                node["is_critical_path"] = tid in critical_path
                node["out_degree"]       = G.out_degree(tid)
                node["in_degree"]        = G.in_degree(tid)

            return {
                "nodes":               list(tasks.values()),
                "edges":               edges,
                "node_count":          len(tasks),
                "edge_count":          len(edges),
                "critical_path":       critical_path,
                "longest_path_length": longest_path_length,
                "bottlenecks":         bottlenecks,
                "is_dag":              nx.is_directed_acyclic_graph(G),
                "has_cycles":          not nx.is_directed_acyclic_graph(G),
            }
        except Exception as e:
            log.error("dag_graph_error", error=str(e))
            return {"nodes": [], "edges": [], "node_count": 0, "edge_count": 0}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# TASK QUEUE â€” Redis sorted set + PostgreSQL fallback
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def enqueue_task(task_id: str, urgency: str = "medium", priority: str = "P2",
                 critical_path: bool = False, entropy: float = 0.0,
                 created_ts: float = 0.0) -> bool:
    r = _r()
    if not r:
        return False
    score = _priority_score(urgency or priority, critical_path, entropy, created_ts)
    try:
        r.zadd(QUEUE_KEY, {task_id: score}, nx=True)
        return True
    except Exception as e:
        log.warning("enqueue_failed", task_id=task_id, error=str(e))
        return False


def dequeue_task(agent_name: str, claim_ttl_s: int = 120) -> Optional[str]:
    r = _r()
    if r:
        try:
            result = r.zpopmax(QUEUE_KEY, count=1)
            if result:
                task_id, _ = result[0]
                r.setex(f"{QUEUE_CLAIMED_PREFIX}{task_id}", claim_ttl_s, agent_name)
                if _HAS_EVENT_STORE:
                    _es_task_claimed(task_id, actor=agent_name, claim_ttl_s=claim_ttl_s)
                return task_id
        except Exception as e:
            log.warning("dequeue_redis_failed", error=str(e), fallback="postgresql")

    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT id, tenant_id, project_id FROM tasks WHERE status = 'pending'
                    ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 ELSE 2 END,
                             critical_path DESC, created_at ASC
                    LIMIT 1 FOR UPDATE SKIP LOCKED
                """)
                row = cur.fetchone()
                if not row:
                    return None
                task_id = row["id"]
                cur.execute("""
                    UPDATE tasks SET status='in-progress', assigned_agent=%s, updated_at=NOW()
                    WHERE id=%s
                """, (agent_name, task_id))
            if _HAS_EVENT_STORE:
                _es_task_claimed(
                    task_id, actor=agent_name, claim_ttl_s=claim_ttl_s,
                    tenant_id=row.get("tenant_id", ""),
                    project_id=row.get("project_id", ""),
                )
            return task_id
    except Exception as e:
        log.error("dequeue_pg_failed", error=str(e))
        return None


def release_task(task_id: str):
    r = _r()
    if r:
        try:
            r.delete(f"{QUEUE_CLAIMED_PREFIX}{task_id}")
        except Exception:
            pass


def remove_from_queue(task_id: str):
    r = _r()
    if r:
        try:
            r.zrem(QUEUE_KEY, task_id)
        except Exception:
            pass
    release_task(task_id)


def queue_depth() -> int:
    r = _r()
    if not r:
        return -1
    try:
        return r.zcard(QUEUE_KEY)
    except Exception:
        return -1


def queue_snapshot(limit: int = 20) -> list[dict]:
    r = _r()
    if not r:
        return []
    try:
        items = r.zrevrange(QUEUE_KEY, 0, limit - 1, withscores=True)
        return [{"task_id": tid, "score": float(sc)} for tid, sc in items]
    except Exception:
        return []


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# AGENT REPUTATION ENGINE
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def load_reputation(tenant_id: str = "") -> list[dict]:
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                where  = "WHERE tenant_id = %s" if tenant_id else ""
                params = [tenant_id] if tenant_id else []
                cur.execute(f"""
                    SELECT agent_name, tasks_total, tasks_success, tasks_failure,
                           runtime_success_rate, reputation_fit_score,
                           backend_affinity, frontend_affinity, db_affinity,
                           arch_affinity, qa_affinity, devops_affinity,
                           runtime_samples, confidence_lower, confidence_upper,
                           is_statistically_valid, updated_at
                    FROM agent_reputation {where} ORDER BY reputation_fit_score DESC
                """, params)
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error("load_reputation_error", error=str(e))
        return []


def score_agent_for_task(agent: dict, task: dict) -> float:
    total   = int(agent.get("tasks_total") or 0)
    success = int(agent.get("tasks_success") or 0)
    samples = int(agent.get("runtime_samples") or total)
    db_cl   = float(agent.get("confidence_lower") or 0.0)

    if samples < MIN_SAMPLES_VALID or not agent.get("is_statistically_valid"):
        w_lower, _ = _wilson(success, total)
        eff_rate   = db_cl if db_cl > 0 else w_lower
        penalty    = 1.0 - (samples / MIN_SAMPLES_VALID) * 0.3
    else:
        eff_rate = float(agent.get("runtime_success_rate") or 0.5)
        penalty  = 1.0

    domain_col = _infer_domain(task.get("title", ""), task.get("description", ""))
    affinity   = float(agent.get(domain_col) or agent.get("reputation_fit_score") or 0.5)
    return eff_rate * penalty + affinity * 0.15


def select_best_agent(task: dict, tenant_id: str = "") -> Optional[str]:
    agents = load_reputation(tenant_id)
    if not agents:
        return None
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT assigned_agent, COUNT(*) active FROM tasks
                    WHERE status='in-progress' AND assigned_agent IS NOT NULL
                    GROUP BY assigned_agent
                """)
                workload = {r["assigned_agent"]: r["active"] for r in cur.fetchall()}
    except Exception:
        workload = {}

    scored = [
        (score_agent_for_task(a, task), a["agent_name"])
        for a in agents
        if workload.get(a["agent_name"], 0) < MAX_AGENT_ACTIVE_TASKS
    ]
    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][1]


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# SCHEDULER â€” APScheduler job (replaces manual thread + lock)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_sched_stats = {"ticks": 0, "promoted": 0, "dispatched": 0, "last_tick": None}


def _scheduler_tick():
    with _span("scheduler.tick"):
        _t0 = time.perf_counter()
        ready    = get_ready_tasks(limit=200)
        promoted = dispatched = 0
        try:
            with _db() as conn:
                with conn.cursor() as cur:
                    for task in ready:
                        tid = task["id"]
                        if task.get("status") == "waiting":
                            cur.execute(
                                "UPDATE tasks SET status='pending', updated_at=NOW() WHERE id=%s",
                                (tid,))
                            promoted += 1
                        if enqueue_task(tid,
                                        urgency      = task.get("urgency") or "medium",
                                        priority     = task.get("priority") or "P2",
                                        critical_path= bool(task.get("critical_path")),
                                        entropy      = float(task.get("entropy_score") or 0.0),
                                        created_ts   = float(task.get("created_ts") or 0.0)):
                            dispatched += 1
        except Exception as e:
            log.error("scheduler_tick_error", error=str(e))

        elapsed = time.perf_counter() - _t0
        _m_tick_latency.observe(elapsed)
        _m_tick_total.inc()
        _m_promoted.inc(promoted)
        _m_dispatched.inc(dispatched)
        _sched_stats.update({
            "ticks":     _sched_stats["ticks"] + 1,
            "promoted":  _sched_stats["promoted"] + promoted,
            "dispatched":_sched_stats["dispatched"] + dispatched,
            "last_tick": datetime.now(timezone.utc).isoformat(),
        })
        if promoted or dispatched:
            log.info("scheduler_tick", promoted=promoted, dispatched=dispatched,
                     depth=queue_depth(), elapsed_ms=round(elapsed * 1000, 1))


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# WATCHDOG â€” APScheduler job (recycles stale in-progress tasks)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_watchdog_stats = {"recycled": 0, "last_sweep": None}


def _watchdog_sweep():
    with _span("watchdog.sweep"):
        threshold = datetime.now(timezone.utc) - timedelta(seconds=STALE_HEARTBEAT_THRESHOLD_S)
        recycled  = 0
        try:
            task_pk = _task_pk_column()
            heartbeat_time_col = _heartbeat_time_column()
            with _db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT t.{task_pk} AS id, t.assigned_agent,
                               COALESCE(t.lock_conflict_count, 0) AS conflict_count
                        FROM tasks t
                        WHERE t.status = 'in-progress'
                          AND NOT EXISTS (
                              SELECT 1 FROM heartbeats h
                              WHERE h.task_id = t.{task_pk} AND h.{heartbeat_time_col} > %s
                          )
                        LIMIT 50
                    """.format(task_pk=task_pk, heartbeat_time_col=heartbeat_time_col), (threshold,))
                    stale = cur.fetchall()

                    for row in stale:
                        tid     = row["id"]
                        backoff = _compute_backoff_until(row["conflict_count"])
                        cur.execute("""
                            UPDATE tasks SET status='pending', assigned_agent=NULL,
                                updated_at=NOW(),
                                lock_conflict_count = COALESCE(lock_conflict_count,0)+1,
                                lock_retry_count    = COALESCE(lock_retry_count,0)+1,
                                lock_backoff_until  = %s
                            WHERE id=%s AND status='in-progress'
                        """, (backoff, tid))
                        cur.execute("""
                            INSERT INTO agent_events (task_id, agent_name, event_type, payload)
                            VALUES (%s, %s, 'repair', %s)
                        """, (tid, row.get("assigned_agent") or "watchdog",
                              json.dumps({"reason": "stale_heartbeat",
                                          "backoff_until": backoff.isoformat()})))
                        remove_from_queue(tid)
                        recycled += 1
                        log.info("watchdog_recycled", task_id=tid,
                                 agent=row.get("assigned_agent"),
                                 backoff_until=backoff.isoformat())

        except Exception as e:
            log.error("watchdog_sweep_error", error=str(e))

        _m_recycled.inc(recycled)
        _watchdog_stats.update({
            "recycled":   _watchdog_stats["recycled"] + recycled,
            "last_sweep": datetime.now(timezone.utc).isoformat(),
        })


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# APScheduler â€” initialized here, started in lifespan
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_bg_scheduler = BackgroundScheduler(
    executors={"default": APThreadPool(max_workers=2)},
    job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 10},
)
_bg_scheduler.add_job(
    _scheduler_tick, "interval",
    seconds=SCHEDULER_INTERVAL_S,
    id="scheduler_tick",
)
_bg_scheduler.add_job(
    _watchdog_sweep, "interval",
    seconds=WATCHDOG_INTERVAL_S,
    id="watchdog_sweep",
)
if _HAS_EVENT_STORE:
    _bg_scheduler.add_job(
        _es_refresh_projections, "interval",
        minutes=int(os.environ.get("PROJECTION_REFRESH_MINUTES", "5")),
        id="refresh_projections",
    )


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# AUTO-SEEDER â€” entropy_snapshots â†’ tasks
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

def seed_from_entropy(project_id: str, tenant_id: str = "local",
                      threshold: float = 0.75) -> list[str]:
    with _span("seeder.entropy"):
        created: list[str] = []
        try:
            with _db() as conn:
                with conn.cursor() as cur:
                    cur.execute("""
                        SELECT DISTINCT ON (es.file_path)
                               es.file_path, es.entropy_score, es.label,
                               COALESCE(es.hotspot_score, 0) AS hotspot_score,
                               COALESCE(es.martin_zone, 'neutral') AS martin_zone,
                               COALESCE(es.instability, 1.0)  AS instability,
                               COALESCE(es.blast_weight, 0.0) AS blast_weight,
                               COALESCE(es.churn_count, 0)    AS churn_count
                        FROM entropy_snapshots es
                        WHERE es.project_id  = %s AND es.tenant_id = %s
                          AND es.entropy_score >= %s
                          AND NOT EXISTS (
                              SELECT 1 FROM tasks t
                              WHERE t.tenant_id = %s AND t.project_id = %s
                                AND t.status NOT IN ('done','cancelled','skipped','failed','dead-letter')
                                AND t.title ILIKE ('Refactor: ' || es.file_path || '%')
                          )
                        ORDER BY es.file_path, es.scan_at DESC LIMIT 25
                    """, (project_id, tenant_id, threshold, tenant_id, project_id))
                    files = cur.fetchall()

                    for f in files:
                        fpath   = f["file_path"]
                        escore  = float(f["entropy_score"])
                        label   = f["label"]
                        hotspot = float(f["hotspot_score"])
                        zone    = f["martin_zone"]
                        urgency = ("critical" if escore >= 0.85 or label == "structural_hazard"
                                   else "high" if escore >= 0.75 else "medium")
                        priority = "P0" if urgency == "critical" else ("P1" if urgency == "high" else "P2")
                        task_id = str(uuid.uuid4())
                        title   = f"Refactor: {fpath.split('/')[-1]}"
                        desc    = (
                            f"**Auto-seeded by entropy scanner.**\n\n"
                            f"| Metric | Value |\n|---|---|\n"
                            f"| File | `{fpath}` |\n"
                            f"| Entropy | {escore:.4f} ({label}) |\n"
                            f"| Hotspot | {hotspot:.4f} |\n"
                            f"| Martin zone | {zone} |\n\n"
                            f"**Action:** Reduce cyclomatic complexity, coupling, and duplication."
                        )
                        cur.execute("""
                            INSERT INTO tasks
                                (id, project_id, tenant_id, title, description,
                                 urgency, priority, status, entropy_score, created_at, updated_at)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,'pending',%s,NOW(),NOW())
                        """, (task_id, project_id, tenant_id, title, desc,
                              urgency, priority, escore))
                        cur.execute("""
                            INSERT INTO agent_events (task_id, agent_name, event_type, payload)
                            VALUES (%s,'core-seeder','dispatch',%s)
                        """, (task_id, json.dumps({
                            "source": "entropy_scanner",
                            "file": fpath, "entropy": escore, "label": label,
                        })))
                        enqueue_task(task_id, urgency, priority, False, escore)
                        created.append(task_id)
                        if _HAS_EVENT_STORE:
                            _es_task_created(
                                task_id, actor="core-seeder", title=title,
                                urgency=urgency, tenant_id=tenant_id, project_id=project_id,
                            )

        except Exception as e:
            log.error("seed_from_entropy_error", error=str(e))

        _m_seeded.inc(len(created))
        if created:
            log.info("entropy_seeded", count=len(created),
                     project=project_id, threshold=threshold)
        return created


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# STORAGE ADAPTER â€” POSIX-first (local | S3 | GitHub)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_SKIP_DIRS = frozenset({
    "vendor", "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", "storage", "bootstrap/cache",
})


class StorageAdapter:
    """POSIX-first workspace abstraction (local | s3 | github)."""

    def __init__(self, backend: str, root: str):
        self.backend = backend
        self.root    = root.rstrip("/")

    @classmethod
    def from_env(cls) -> "StorageAdapter":
        return cls(os.environ.get("WORKSPACE_BACKEND", "local"),
                   os.environ.get("WORKSPACE_ROOT", "/workspace"))

    def _posix(self, path: str) -> str:
        import pathlib
        return str(pathlib.PurePosixPath(path.replace("\\", "/").lstrip("/")))

    def _local_abs(self, path: str) -> str:
        import pathlib
        return str(pathlib.Path(self.root) / self._posix(path))

    def read(self, path: str) -> str:
        if self.backend == "local":
            with open(self._local_abs(path), encoding="utf-8") as fh:
                return fh.read()
        if self.backend == "s3":
            return self._s3_read(path)
        if self.backend == "github":
            return self._github_read(path)
        raise ValueError(f"Unknown backend: {self.backend}")

    def write(self, path: str, content: str) -> None:
        if self.backend == "local":
            import pathlib
            p = pathlib.Path(self._local_abs(path))
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return
        if self.backend == "s3":
            self._s3_write(path, content); return
        if self.backend == "github":
            self._github_write(path, content); return
        raise ValueError(f"Unknown backend: {self.backend}")

    def list(self, prefix: str = "", extensions: Optional[list[str]] = None) -> list[str]:
        if self.backend == "local":
            return self._local_list(prefix, extensions)
        if self.backend == "s3":
            return self._s3_list(prefix)
        if self.backend == "github":
            return self._github_list(prefix)
        raise ValueError(f"Unknown backend: {self.backend}")

    def exists(self, path: str) -> bool:
        if self.backend == "local":
            import pathlib
            return pathlib.Path(self._local_abs(path)).exists()
        if self.backend == "s3":
            return self._s3_exists(path)
        if self.backend == "github":
            try:
                self._github_read(path); return True
            except Exception:
                return False
        return False

    def stat(self, path: str) -> dict:
        if self.backend == "local":
            import pathlib
            st = pathlib.Path(self._local_abs(path)).stat()
            return {"path": path, "size": st.st_size,
                    "modified_at": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()}
        return {"path": path, "size": -1, "modified_at": None}

    def _local_list(self, prefix: str = "", extensions: Optional[list[str]] = None) -> list[str]:
        import pathlib
        root = pathlib.Path(self.root)
        base = (root / self._posix(prefix)) if prefix else root
        if not base.exists():
            return []
        result = []
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            if any(d in _SKIP_DIRS for d in p.parts):
                continue
            if extensions and p.suffix.lower() not in extensions:
                continue
            result.append(str(p.relative_to(root)).replace("\\", "/"))
        return sorted(result)

    def _s3_parts(self) -> tuple[str, str]:
        import urllib.parse
        p = urllib.parse.urlparse(self.root)
        return p.netloc, p.path.lstrip("/")

    def _s3_key(self, path: str) -> str:
        _, prefix = self._s3_parts()
        return f"{prefix}/{self._posix(path)}" if prefix else self._posix(path)

    def _s3_client(self):
        import boto3
        return boto3.client("s3")

    def _s3_read(self, path: str) -> str:
        bucket, _ = self._s3_parts()
        obj = self._s3_client().get_object(Bucket=bucket, Key=self._s3_key(path))
        return obj["Body"].read().decode("utf-8")

    def _s3_write(self, path: str, content: str) -> None:
        bucket, _ = self._s3_parts()
        self._s3_client().put_object(
            Bucket=bucket, Key=self._s3_key(path),
            Body=content.encode("utf-8"), ContentType="text/plain")

    def _s3_list(self, prefix: str = "") -> list[str]:
        bucket, root_prefix = self._s3_parts()
        key_prefix = f"{root_prefix}/{self._posix(prefix)}" if prefix else root_prefix
        pag    = self._s3_client().get_paginator("list_objects_v2")
        result = []
        for page in pag.paginate(Bucket=bucket, Prefix=key_prefix):
            for obj in page.get("Contents", []):
                rel = obj["Key"].removeprefix(root_prefix).lstrip("/")
                if rel:
                    result.append(rel)
        return sorted(result)

    def _s3_exists(self, path: str) -> bool:
        import botocore.exceptions
        bucket, _ = self._s3_parts()
        try:
            self._s3_client().head_object(Bucket=bucket, Key=self._s3_key(path))
            return True
        except botocore.exceptions.ClientError:
            return False

    def _gh_headers(self) -> dict:
        token = os.environ.get("GITHUB_TOKEN", "")
        h = {"Accept": "application/vnd.github.v3+json",
             "User-Agent": "sinc-orchestrator-core/4"}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _gh_repo_ref(self) -> tuple[str, str]:
        repo, _, ref = self.root.partition("@")
        return repo, ref or "main"

    def _gh_api(self, endpoint: str) -> dict:
        import urllib.request
        req = urllib.request.Request(
            f"https://api.github.com/{endpoint.lstrip('/')}",
            headers=self._gh_headers())
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())

    def _github_read(self, path: str) -> str:
        repo, ref = self._gh_repo_ref()
        data = self._gh_api(f"repos/{repo}/contents/{self._posix(path)}?ref={ref}")
        return base64.b64decode(data["content"]).decode("utf-8")

    def _github_write(self, path: str, content: str) -> None:
        import urllib.request
        repo, ref = self._gh_repo_ref()
        encoded   = base64.b64encode(content.encode()).decode()
        sha: Optional[str] = None
        try:
            existing = self._gh_api(f"repos/{repo}/contents/{self._posix(path)}?ref={ref}")
            sha = existing.get("sha")
        except Exception:
            pass
        payload = {"message": f"chore: update {path} via orchestrator-core",
                   "content": encoded, "branch": ref}
        if sha:
            payload["sha"] = sha
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/contents/{self._posix(path)}",
            data=json.dumps(payload).encode(), method="PUT",
            headers={**self._gh_headers(), "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=15):
            pass

    def _github_list(self, prefix: str = "") -> list[str]:
        repo, ref = self._gh_repo_ref()
        tree = self._gh_api(f"repos/{repo}/git/trees/{ref}?recursive=1")
        result = []
        for item in tree.get("tree", []):
            if item.get("type") != "blob":
                continue
            p = item["path"]
            if prefix and not p.startswith(self._posix(prefix)):
                continue
            result.append(p)
        return sorted(result)


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# FASTAPI APPLICATION  (replaces Flask)
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Startup: OTEL + scheduler.  Shutdown: scheduler + DB pool."""
    # â”€â”€ startup â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    configure_otel(OTEL_SERVICE_NAME)
    instrument_psycopg()
    instrument_redis()
    instrument_fastapi(app)
    _bg_scheduler.start()
    log.info("apscheduler_started",
             scheduler_interval=SCHEDULER_INTERVAL_S,
             watchdog_interval=WATCHDOG_INTERVAL_S)

    yield  # â”€â”€ app running â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

    # â”€â”€ shutdown â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
    _bg_scheduler.shutdown(wait=False)
    if _pool is not None:
        _pool.close()
    log.info("core_shutdown")


app = FastAPI(
    title="Orchestrator Core",
    version="4.0",
    description="Central coordination service â€” DAG scheduler, queue, reputation, workspace",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _trace_header_middleware(request: Request, call_next):
    trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Trace-Id"]     = trace_id
    response.headers["X-Core-Version"] = "4.0"
    _m_http_reqs.labels(
        method=request.method,
        path=request.url.path,
        status=str(response.status_code),
    ).inc()
    return response


# â”€â”€ Health / Readiness â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

_AUTH = [Depends(_auth_only)]  # shorthand used on every protected route


@app.get("/health", dependencies=_AUTH)
def health():
    with _span("api.health"):
        try:
            db_ok = _db_ok()
        except Exception:
            db_ok = False
        redis_ok = _redis_ok()
        status   = "ok" if db_ok else "degraded"
        try:
            pool_stats = _get_pool().get_stats()
        except Exception:
            pool_stats = {}
        payload = {
            "status":      status,
            "db":          db_ok,
            "redis":       redis_ok,
            "redis_mode":  "active" if redis_ok else "fallback-pg",
            "queue_depth": queue_depth(),
            "scheduler":   _sched_stats,
            "watchdog":    _watchdog_stats,
            "db_pool":     pool_stats,
            "ts":          datetime.now(timezone.utc).isoformat(),
        }
        return JSONResponse(content=payload, status_code=200 if status == "ok" else 503)


@app.get("/ready")
def ready():
    try:
        ok = _db_ok()
    except Exception:
        ok = False
    return Response(status_code=200 if ok else 503)


# â”€â”€ Prometheus metrics â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/metrics")
def metrics():
    _refresh_metrics()
    return Response(content=generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)


# â”€â”€ Queue â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/queue", dependencies=_AUTH)
def api_queue_status():
    return {"depth": queue_depth(), "top": queue_snapshot(20),
            "backend": "redis" if _redis_ok() else "postgresql"}


@app.post("/queue/enqueue", dependencies=_AUTH)
def api_queue_enqueue(body: EnqueueBody):
    with _span("api.queue.enqueue"):
        ok = enqueue_task(body.task_id, body.urgency, body.priority,
                          body.critical_path, body.entropy_score, time.time())
        return {"enqueued": ok, "depth": queue_depth()}


@app.post("/queue/dequeue", dependencies=_AUTH)
def api_queue_dequeue(body: DequeueBody):
    with _span("api.queue.dequeue"):
        task_id = dequeue_task(body.agent_name, body.claim_ttl_s)
        if not task_id:
            return JSONResponse(content={"task_id": None}, status_code=204)
        return {"task_id": task_id}


class PollBody(BaseModel):
    agent_name:  str = "unknown"
    claim_ttl_s: int = Field(120, ge=10, le=3600)
    timeout_s:   int = Field(30, ge=1, le=120)


@app.post("/queue/poll", dependencies=_AUTH)
async def api_queue_poll(body: PollBody):
    """Long-poll: block up to timeout_s seconds waiting for a task."""
    deadline = asyncio.get_event_loop().time() + body.timeout_s
    while True:
        task_id = dequeue_task(body.agent_name, body.claim_ttl_s)
        if task_id:
            return {"task_id": task_id}
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            return JSONResponse(content={"task_id": None}, status_code=204)
        await asyncio.sleep(min(2.0, remaining))


@app.post("/queue/release/{task_id}", dependencies=_AUTH)
def api_queue_release(task_id: str):
    remove_from_queue(task_id)
    return {"released": task_id}


@app.delete("/queue/{task_id}", dependencies=_AUTH)
def api_queue_delete(task_id: str):
    remove_from_queue(task_id)
    return {"removed": task_id}


# â”€â”€ DAG â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/dag/ready", dependencies=_AUTH)
def api_dag_ready(
    limit: int = Query(50, ge=1, le=500),
    tenant_id: str = Depends(_get_tenant),
):
    with _span("api.dag.ready"):
        ready = get_ready_tasks(tenant_id, limit)
        return {"ready": ready, "count": len(ready)}


@app.get("/dag/blocked", dependencies=_AUTH)
def api_dag_blocked(tenant_id: str = Depends(_get_tenant)):
    blocked = get_blocked_tasks(tenant_id)
    return {"blocked": blocked, "count": len(blocked)}


@app.get("/dag/graph", dependencies=_AUTH)
def api_dag_graph(
    project_id: str = Query(""),
    tenant_id: str = Depends(_get_tenant),
):
    with _span("api.dag.graph"):
        return dag_graph(tenant_id, project_id)


@app.post("/dag/tick", dependencies=_AUTH)
def api_dag_tick():
    with _span("api.dag.tick"):
        before = queue_depth()
        _scheduler_tick()
        return {"before": before, "after": queue_depth(), "stats": _sched_stats}


# â”€â”€ Scheduler / Watchdog â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/scheduler/status", dependencies=_AUTH)
def api_scheduler_status():
    jobs = [{"id": j.id, "next_run": str(j.next_run_time)} for j in _bg_scheduler.get_jobs()]
    return {
        "scheduler": _sched_stats,
        "watchdog":  _watchdog_stats,
        "jobs":      jobs,
        "config": {
            "scheduler_interval_s": SCHEDULER_INTERVAL_S,
            "watchdog_interval_s":  WATCHDOG_INTERVAL_S,
            "stale_threshold_s":    STALE_HEARTBEAT_THRESHOLD_S,
        },
    }


@app.post("/scheduler/watchdog", dependencies=_AUTH)
def api_watchdog():
    before = _watchdog_stats["recycled"]
    _watchdog_sweep()
    return {"recycled": _watchdog_stats["recycled"] - before,
            "total":    _watchdog_stats["recycled"]}


# â”€â”€ Auto-seeder â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.post("/seed/entropy", dependencies=_AUTH)
def api_seed_entropy(body: SeedEntropyBody, tenant_id: str = Depends(_get_tenant)):
    with _span("api.seed.entropy"):
        _validate_id(body.project_id, "project_id")
        created = seed_from_entropy(body.project_id, tenant_id, body.threshold)
        return {"created": created, "count": len(created)}


# â”€â”€ Reputation â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/reputation", dependencies=_AUTH)
def api_reputation():
    agents = load_reputation()
    return {"agents": agents, "count": len(agents)}


@app.get("/reputation/best", dependencies=_AUTH)
def api_best_agent(
    title: str = Query(""),
    description: str = Query(""),
    tenant_id: str = Depends(_get_tenant),
):
    name = select_best_agent({"title": title, "description": description}, tenant_id)
    return {"best_agent": name, "domain": _infer_domain(title, description)}


@app.get("/reputation/{agent_name}", dependencies=_AUTH)
def api_reputation_agent(agent_name: str):
    match = next((a for a in load_reputation() if a["agent_name"] == agent_name), None)
    if not match:
        raise HTTPException(status_code=404, detail="agent not found")
    return match


@app.post("/reputation/score", dependencies=_AUTH)
def api_score_task(body: ScoreTaskBody):
    task   = {"title": body.title, "description": body.description}
    agents = load_reputation()
    ranked = sorted(
        [{"agent": a["agent_name"], "score": round(score_agent_for_task(a, task), 4)}
         for a in agents],
        key=lambda x: x["score"], reverse=True,
    )
    return {"task": task,
            "domain": _infer_domain(body.title, body.description),
            "ranked": ranked}


# â”€â”€ Workspace â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

@app.get("/workspace", dependencies=_AUTH)
def api_workspace_info():
    a = StorageAdapter.from_env()
    return {"backend": a.backend, "root": a.root}


@app.get("/workspace/list", dependencies=_AUTH)
def api_workspace_list(
    prefix: str = Query(""),
    ext: list[str] = Query(default=[]),
):
    adapter = StorageAdapter.from_env()
    try:
        files = adapter.list(prefix, ext or None)
        return {"files": files, "count": len(files), "backend": adapter.backend}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/workspace/read", dependencies=_AUTH)
def api_workspace_read(path: str = Query("")):
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    adapter = StorageAdapter.from_env()
    try:
        return {"path": path, "content": adapter.read(path), "backend": adapter.backend}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workspace/write", dependencies=_AUTH)
def api_workspace_write(body: WorkspaceWriteBody):
    adapter = StorageAdapter.from_env()
    try:
        adapter.write(body.path, body.content)
        return {"written": body.path, "backend": adapter.backend}
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/workspace/exists", dependencies=_AUTH)
def api_workspace_exists(path: str = Query("")):
    adapter = StorageAdapter.from_env()
    return {"path": path, "exists": adapter.exists(path), "backend": adapter.backend}


@app.get("/workspace/stat", dependencies=_AUTH)
def api_workspace_stat(path: str = Query("")):
    if not path:
        raise HTTPException(status_code=400, detail="path required")
    adapter = StorageAdapter.from_env()
    try:
        return adapter.stat(path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="not found")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# â”€â”€ Event Store API â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if _HAS_EVENT_STORE:
    from services.event_store import replay as _es_replay, project_state as _es_state, llm_lineage as _es_lineage

    @app.get("/events/{task_id}", dependencies=_AUTH)
    def api_events_replay(task_id: str):
        """Return full ordered event stream for a task (deterministic replay)."""
        events = list(_es_replay(task_id))
        return {"task_id": task_id, "events": events, "count": len(events)}

    @app.get("/events/{task_id}/state", dependencies=_AUTH)
    def api_events_state(task_id: str):
        """Return current projected state derived from events (mv_task_projection)."""
        return _es_state(task_id) or {}

    @app.get("/events/{task_id}/llm", dependencies=_AUTH)
    def api_events_llm(task_id: str):
        """Return all LLM calls for a task with prompts/responses (dataset generation)."""
        rows = _es_lineage(task_id)
        return {"task_id": task_id, "llm_calls": rows, "count": len(rows)}

    @app.post("/events/refresh", dependencies=_AUTH)
    def api_events_refresh():
        """Manually trigger a refresh of all event sourcing materialized views."""
        from services.event_store import refresh_projections
        refresh_projections()
        return {"refreshed": True}


# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
# ENTRYPOINT
# â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€

if __name__ == "__main__":
    log.info("core_starting",
             host=CORE_HOST, port=CORE_PORT,
             redis=f"{REDIS_HOST}:{REDIS_PORT}/db{REDIS_DB}",
             workspace_backend=os.environ.get("WORKSPACE_BACKEND", "local"),
             workspace_root=os.environ.get("WORKSPACE_ROOT", "/workspace"),
             otel=bool(OTEL_OTLP_ENDPOINT))
    uvicorn.run(
        "orchestrator_core:app",
        host=CORE_HOST,
        port=CORE_PORT,
        log_level="warning",
        access_log=False,
        workers=1,
    )

