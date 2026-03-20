"""
streaming package bootstrap for the Python control plane.

This module is intentionally strict: critical routers and startup
dependencies must load successfully, otherwise the service must fail fast.
"""

from __future__ import annotations
from services.streaming.core.config import env_get

import asyncio
import importlib
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .core.config import CORS_ORIGINS
from .core import auth as _auth  # imported for side effects/context wiring
from .core import sse as _sse  # imported for side effects/context wiring
from .core.db import async_db, get_async_pool, get_pool
from .core.runtime_plane import ensure_runtime_plane_schema

_LOG = logging.getLogger("orchestrator")

_CRITICAL_ROUTERS = (
    "health",
    "events",
    "tasks",
    "agents",
    "dashboard",
    "dashboard_api",
)

_OPTIONAL_ROUTERS = (
    "admin",
    "legacy_compat",
    "core_compat",
    "projects",
    "ingest",
    "entropy",
    "twin",
    "simulate",
    "plans",
    "gates",
    "usage",
    "connect",
    "ask",
    "system",
    "misc",
    "cognitive",
    "intelligence",
    "analytics",
    "system_infra",
    "intelligence_v1",
)


def _register_router(
    app: FastAPI,
    module_name: str,
    *,
    critical: bool,
    bootstrap_status: dict[str, Any],
) -> None:
    module_path = f"{__package__}.routes.{module_name}"
    try:
        module = importlib.import_module(module_path)
        router = getattr(module, "router", None)
        if router is None:
            raise RuntimeError("module has no 'router' attribute")
        app.include_router(router)
        bootstrap_status["routers"][module_name] = {
            "status": "loaded",
            "critical": critical,
            "module": module_path,
        }
    except Exception as exc:
        bootstrap_status["routers"][module_name] = {
            "status": "failed",
            "critical": critical,
            "module": module_path,
            "error": str(exc),
        }
        if critical:
            raise RuntimeError(
                f"critical_router_registration_failed: {module_name}: {exc}"
            ) from exc
        _LOG.warning("router_register_failed name=%s error=%s", module_name, exc)


def _spawn_background_task(app: FastAPI, coro, *, name: str) -> None:
    task = asyncio.create_task(coro, name=name)
    app.state.background_tasks.append(task)
    app.state.bootstrap_status["background_tasks"][name] = "running"

    def _done_callback(done_task: asyncio.Task) -> None:
        try:
            done_task.result()
        except asyncio.CancelledError:
            _LOG.info("background_task_cancelled name=%s", name)
        except Exception as exc:
            _LOG.error(
                "background_task_failed name=%s error=%s",
                name,
                exc,
                exc_info=True,
            )
            app.state.bootstrap_status["background_tasks"][name] = f"failed:{exc}"
        else:
            app.state.bootstrap_status["background_tasks"][name] = "completed"

    task.add_done_callback(_done_callback)


async def _run_startup(app: FastAPI) -> None:
    import os

    if env_get("APP_ENV") == "production" and not env_get("REDIS_PASSWORD"):
        _LOG.warning(
            "security_warning APP_ENV=production but REDIS_PASSWORD is unset - "
            "Redis traffic is unauthenticated"
        )

    admin_key = env_get("ADMIN_API_KEY", default="")
    is_prod = env_get("APP_ENV") == "production"
    if is_prod:
        if not admin_key or admin_key == "sk-admin-change-me":
            _LOG.critical(
                "security_failure ADMIN_API_KEY must be set to a strong value in production"
            )
            raise RuntimeError(
                "PRODUCTION_SECURITY_VIOLATION: ADMIN_API_KEY is missing or default"
            )
    elif admin_key in ("", "sk-admin-change-me"):
        _LOG.warning(
            "security_warning ADMIN_API_KEY is set to the default value - "
            "change it before exposing this service"
        )

    await _verify_startup_dependencies(app)
    await ensure_runtime_plane_schema()

    from .core.state_plane import run_task_dag_projection_loop, sync_task_dag_projection
    from .core.watchdog import run_watchdog
    from .core.external_agent_bridge import run_external_bridge_loop
    from .core.governance_plane import (
        ensure_governance_schema,
        run_deploy_verify_loop,
        run_finops_loop,
        run_mutation_loop,
        run_pattern_promotion_loop,
        run_policy_loop,
        run_release_loop,
    )
    from .core.runtime_plane import run_observer_loop, run_readiness_loop, run_scheduler_loop
    from .routes.cognitive import run_cognitive_batch_queue_loop
    from .routes.dashboard_api import run_diagnostic_log_projection_loop, run_telemetry_broadcaster

    await ensure_governance_schema()
    if env_get("ORCHESTRATOR_EMBEDDED_TASK_DAG_PROJECTION_ENABLED", default="1") != "0":
        await sync_task_dag_projection()

    _spawn_background_task(app, run_watchdog(), name="watchdog")
    if env_get("ORCHESTRATOR_EMBEDDED_SCHEDULER_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_scheduler_loop(), name="scheduler_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_OBSERVER_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_observer_loop(), name="observer_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_READINESS_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_readiness_loop(), name="readiness_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_EXTERNAL_BRIDGE_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_external_bridge_loop(), name="external_agent_bridge_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_POLICY_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_policy_loop(), name="policy_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_MUTATION_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_mutation_loop(), name="mutation_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_FINOPS_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_finops_loop(), name="finops_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_DEPLOY_VERIFY_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_deploy_verify_loop(), name="deploy_verify_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_PATTERN_PROMOTION_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_pattern_promotion_loop(), name="pattern_promotion_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_RELEASE_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_release_loop(), name="release_worker")
    if env_get("ORCHESTRATOR_EMBEDDED_TASK_DAG_PROJECTION_ENABLED", default="1") != "0":
        _spawn_background_task(app, run_task_dag_projection_loop(), name="task_dag_projection_worker")
    _spawn_background_task(app, _rule_learner_task(), name="rule_learner")
    _spawn_background_task(
        app,
        _prediction_view_refresh_task(),
        name="prediction_view_refresh",
    )
    if env_get("ORCHESTRATOR_EMBEDDED_LEADERBOARD_FLUSH_ENABLED", default="0") != "0":
        _spawn_background_task(
            app,
            _leaderboard_flush_task(),
            name="leaderboard_flush",
        )
    if env_get("ORCHESTRATOR_EMBEDDED_COGNITIVE_BATCH_ENABLED", default="1") != "0":
        _spawn_background_task(
            app,
            run_cognitive_batch_queue_loop(),
            name="cognitive_batch_queue_worker",
        )
    if env_get("ORCHESTRATOR_EMBEDDED_DASHBOARD_BROADCASTER_ENABLED", default="1") != "0":
        _spawn_background_task(
            app,
            run_telemetry_broadcaster(),
            name="dashboard_telemetry_broadcaster",
        )
    if env_get("ORCHESTRATOR_EMBEDDED_DIAGNOSTIC_LOG_PROJECTION_ENABLED", default="1") != "0":
        _spawn_background_task(
            app,
            run_diagnostic_log_projection_loop(),
            name="diagnostic_log_projection_worker",
        )

    _LOG.info("FastAPI application started")


async def _run_shutdown(app: FastAPI) -> None:
    for task in list(app.state.background_tasks):
        if not task.done():
            task.cancel()
    if app.state.background_tasks:
        await asyncio.gather(*app.state.background_tasks, return_exceptions=True)

    pool = get_pool()
    if pool:
        pool.close()
    apool = get_async_pool()
    if apool:
        await apool.close()
    _LOG.info("FastAPI application shutting down")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    await _run_startup(app)
    try:
        yield
    finally:
        await _run_shutdown(app)


async def _verify_startup_dependencies(app: FastAPI) -> None:
    checks: dict[str, str] = {}

    try:
        get_pool()
        get_async_pool()
        async with async_db(bypass_rls=True) as conn:
            async with conn.cursor() as cur:
                await cur.execute("SELECT 1")
                await cur.fetchone()
        checks["postgres"] = "ok"
    except Exception as exc:
        checks["postgres"] = f"error:{exc}"

    try:
        from services.event_bus import EventBus

        event_bus = await EventBus.get_instance()
        await event_bus.connect()
        checks["event_bus"] = "ok"
    except Exception as exc:
        checks["event_bus"] = f"error:{exc}"

    app.state.bootstrap_status["startup_checks"] = checks

    failed_checks = {name: status for name, status in checks.items() if status != "ok"}
    if failed_checks:
        summary = ", ".join(f"{name}={status}" for name, status in failed_checks.items())
        raise RuntimeError(f"startup_dependency_check_failed: {summary}")


def create_app() -> FastAPI:
    """
    Application factory.

    The new control plane treats router registration and startup dependency
    validation as first-class bootstrap work. Critical failures stop startup.
    """
    app = FastAPI(
        title="SINC Streaming Server",
        version="3.0.0",
        lifespan=_lifespan,
    )
    app.state.background_tasks = []
    app.state.bootstrap_status = {"routers": {}, "background_tasks": {}, "startup_checks": {}}

    from services.otel_setup import (
        configure_otel,
        instrument_fastapi,
        instrument_httpx,
        instrument_psycopg,
        instrument_redis,
    )

    instrument_fastapi(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS.split(",") if CORS_ORIGINS != "*" else ["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Trace-Id"],
    )

    import os
    static_path = os.path.join(os.path.dirname(__file__), "static")
    if os.path.exists(static_path):
        app.mount("/static", StaticFiles(directory=static_path), name="static")

    @app.middleware("http")
    async def global_middleware(request, call_next):
        import uuid
        from .core.auth import set_trace_id

        trace_id = request.headers.get("X-Trace-Id") or uuid.uuid4().hex[:16]
        request.state.trace_id = trace_id
        set_trace_id(trace_id)

        response = await call_next(request)
        response.headers["X-Trace-Id"] = trace_id
        return response

    @app.exception_handler(Exception)
    async def global_exception_handler(request, exc):
        from .core.auth import get_trace_id
        trace_id = get_trace_id()
        _LOG.exception("unhandled_exception trace_id=%s path=%s error=%s", trace_id, request.url.path, exc)
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": "Internal Server Error",
                "message": str(exc),
                "trace_id": trace_id
            }
        )

    for module_name in _CRITICAL_ROUTERS:
        _register_router(
            app,
            module_name,
            critical=True,
            bootstrap_status=app.state.bootstrap_status,
        )

    for module_name in _OPTIONAL_ROUTERS:
        _register_router(
            app,
            module_name,
            critical=False,
            bootstrap_status=app.state.bootstrap_status,
        )

    return app


async def _prediction_view_refresh_task():
    """Refresh task_success_prediction materialized view every 5 minutes."""
    await asyncio.sleep(60)
    while True:
        try:
            from .core.db import async_db as _async_db

            async with _async_db(bypass_rls=True) as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "REFRESH MATERIALIZED VIEW CONCURRENTLY task_success_prediction"
                    )
            _LOG.debug("prediction_view_refreshed")
        except Exception as exc:
            _LOG.debug("prediction_view_refresh_error error=%s", exc)
        await asyncio.sleep(300)


async def _leaderboard_flush_task():
    """
    Flush Redis leaderboard scores to agent_reputation.semantic_score.

    This keeps SQL-based recommendation endpoints aligned with the live Redis
    leaderboard without requiring the dashboard to invent values.
    """
    await asyncio.sleep(90)
    while True:
        try:
            from .core.redis_ import get_async_redis

            redis_client = get_async_redis()
            if redis_client:
                keys = await redis_client.keys("sinc:leaderboard:*")
                for key in keys:
                    parts = key.split(":")
                    if len(parts) < 4:
                        continue
                    tenant_id = parts[2]
                    entries = await redis_client.zrange(key, 0, -1, withscores=True)
                    if not entries:
                        continue
                    async with async_db(bypass_rls=True) as conn:
                        async with conn.cursor() as cur:
                            for agent_name, score in entries:
                                await cur.execute(
                                    """
                                    UPDATE agent_reputation
                                       SET semantic_score = %s,
                                           updated_at = NOW()
                                     WHERE agent_name = %s
                                       AND tenant_id = %s
                                    """,
                                    (round(score, 4), agent_name, tenant_id),
                                )
            _LOG.debug("leaderboard_flushed")
        except Exception as exc:
            _LOG.debug("leaderboard_flush_error error=%s", exc)
        await asyncio.sleep(600)


async def _rule_learner_task():
    """Async wrapper for rule learner."""
    await asyncio.sleep(120)
    while True:
        try:
            def _sync_learn():
                from ..cognitive_orchestrator import get_orchestrator

                orch = get_orchestrator()
                orch.trigger_rule_learning()

            await asyncio.to_thread(_sync_learn)
            _LOG.debug("rule_learner_cycle_done")
        except Exception as exc:
            _LOG.debug("rule_learner_error error=%s", exc)
        await asyncio.sleep(300)
