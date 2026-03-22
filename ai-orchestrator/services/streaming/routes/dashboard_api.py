from __future__ import annotations
from services.streaming.core.config import env_get, OLLAMA_HOST as _OLLAMA_HOST

import asyncio
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
import psutil

import time as _time
from fastapi import APIRouter, Depends, HTTPException, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from services.event_bus import get_event_bus

from services.http_client import create_resilient_client
from services.streaming.core.config import TASK_STALE_TIMEOUT_M
from services.streaming.core.db import async_db
from services.streaming.core.log_diagnostics import (
    extract_log_timestamp as _extract_log_timestamp,
    log_fingerprint as _log_fingerprint,
    log_level_from_line as _log_level_from_line,
)
from services.streaming.core.redis_ import get_async_redis
from services.streaming.core.schema_compat import (
    get_table_columns_cached,
    get_task_pk_column,
)

router = APIRouter(prefix="/api/v5/dashboard", tags=["dashboard_api"])
log = logging.getLogger("orchestrator.dashboard_api")

from services.ast_analyzer import ASTAnalyzer
from services.impact_analyzer import ImpactAnalyzer

# ── System metrics cache (avoids nvidia-smi + psutil on every poll) ──────────
_sysmetrics_cache: dict = {}
_sysmetrics_cache_ts: float = 0.0
_SYSMETRICS_TTL = 10.0  # seconds


@router.get("/system-metrics")
async def dashboard_system_metrics(
    tenant_id: str = Query(default="default"),
):
    """Return real CPU, RAM, disk, GPU usage + task counts for the NOC gauges.
    Results cached for 10 s so rapid polling doesn't spawn nvidia-smi repeatedly."""
    global _sysmetrics_cache, _sysmetrics_cache_ts
    import psutil

    now = _time.time()
    hw = _sysmetrics_cache if (now - _sysmetrics_cache_ts) < _SYSMETRICS_TTL else {}

    if not hw:
        cpu  = psutil.cpu_percent(interval=0.05)
        ram  = psutil.virtual_memory().percent
        disk = psutil.disk_usage("/").percent

        gpu = gpu_temp_c = vram_used_mb = vram_total_mb = None
        try:
            import subprocess as _sp
            result = _sp.run(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,temperature.gpu,memory.used,memory.total",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                parts = [p.strip() for p in result.stdout.strip().split("\n")[0].split(",")]
                if len(parts) >= 1: gpu           = float(parts[0])
                if len(parts) >= 2: gpu_temp_c    = float(parts[1])
                if len(parts) >= 3: vram_used_mb  = float(parts[2])
                if len(parts) >= 4: vram_total_mb = float(parts[3])
        except Exception:
            pass

        disk_read_mb = disk_write_mb = net_recv_mb = net_sent_mb = None
        try:
            _dio = psutil.disk_io_counters()
            if _dio:
                disk_read_mb  = round(_dio.read_bytes  / 1048576, 1)
                disk_write_mb = round(_dio.write_bytes / 1048576, 1)
        except Exception:
            pass
        try:
            _nio = psutil.net_io_counters()
            if _nio:
                net_recv_mb = round(_nio.bytes_recv / 1048576, 1)
                net_sent_mb = round(_nio.bytes_sent / 1048576, 1)
        except Exception:
            pass

        hw = dict(
            cpu=round(cpu, 1), ram=round(ram, 1), disk=round(disk, 1),
            gpu=round(gpu, 1) if gpu is not None else None,
            gpu_temp_c=gpu_temp_c, vram_used_mb=vram_used_mb, vram_total_mb=vram_total_mb,
            vram_pct=round(vram_used_mb / vram_total_mb * 100, 1) if vram_used_mb and vram_total_mb else None,
            disk_read_mb=disk_read_mb, disk_write_mb=disk_write_mb,
            net_recv_mb=net_recv_mb, net_sent_mb=net_sent_mb,
        )
        _sysmetrics_cache    = hw  # noqa: F841
        _sysmetrics_cache_ts = now  # noqa: F841

    # Task counts — NOT cached (needs to be live)
    counts: dict = {"running": 0, "pending": 0, "completed_today": 0, "zombie": 0, "total_today": 0, "tokens_today": 0}
    tasks_per_hour = 0
    try:
        async with async_db() as cur:
            if await _table_exists(cur, "tasks"):
                await cur.execute(
                    """SELECT
                          COUNT(*) FILTER (WHERE status = 'running')                        AS running,
                          COUNT(*) FILTER (WHERE status = 'pending')                        AS pending,
                          COUNT(*) FILTER (WHERE status IN ('done','completed','success')
                                       AND updated_at >= NOW() - INTERVAL '24 hours')       AS completed_today,
                          COUNT(*) FILTER (WHERE status = 'running'
                                       AND updated_at < NOW() - INTERVAL '10 minutes')      AS zombie,
                          COUNT(*) FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours') AS total_today,
                          COALESCE(SUM(tokens_used)
                                   FILTER (WHERE created_at >= NOW() - INTERVAL '24 hours'),
                                   0)                                                       AS tokens_today,
                          COUNT(*) FILTER (WHERE updated_at >= NOW() - INTERVAL '1 hour')   AS tasks_last_hour
                      FROM tasks WHERE tenant_id = %s""",
                    (tenant_id,),
                )
                row = await cur.fetchone()
                if row:
                    tasks_per_hour = int(row.get("tasks_last_hour") or 0)
                    counts = {k: v for k, v in dict(row).items() if k != "tasks_last_hour"}
    except Exception as _exc:
        log.debug("system_metrics_task_count error=%s", _exc)

    return {
        **hw,
        "tasks":          counts,
        "tasks_per_hour": tasks_per_hour,
        "counts":         counts,   # legacy compat
    }


@router.get("/cognitive/blast-radius")
async def get_blast_radius(symbol: str, tenant_id: str = Query(default="default")):
    """Fetches the blast radius to project structural impacts on the NOC dashboard."""
    with ASTAnalyzer() as analyzer:
        driver = analyzer._get_driver()
        if not driver:
            raise HTTPException(status_code=503, detail="Neo4j Graph unavailable")
            
        impact_svc = ImpactAnalyzer(driver)
        result = impact_svc.analyze_impact(symbol, project_id="default", tenant_id=tenant_id)
        return result

from services.context_retriever import ContextRetriever
@router.get("/cognitive/memory/search")
async def search_memory(query: str, project_id: str = "sinc", tenant_id: str = Query(default="default")):
    """Busca vetorial na memoria L3 (Qdrant) para a Command Palette e Search Bar."""
    try:
        retriever = ContextRetriever()
        result = retriever.retrieve(query=query, project_id=project_id, tenant_id=tenant_id, top_k=5)
        cache_hit = retriever.check_semantic_cache(query=query, project_id=project_id, tenant_id=tenant_id, threshold=0.7)
        return {"ok": True, "cache_hit": cache_hit, "result": result}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@router.websocket("/ws/telemetry")
async def websocket_telemetry(
    websocket: WebSocket,
    tenant_id: str = "default"  # Simplified for now, should use a secure token handshake
):
    """
    Real-time telemetry stream via Redis Pub/Sub.
    Eliminates the need for 5s polling on the NOC dashboard.
    """
    await websocket.accept()
    bus = await get_event_bus()
    
    # Subscribe to multiple channels relevant to the dashboard
    channels = ["metrics", "tasks", "alerts", "agent_events"]
    
    async def _send_to_ws(data: dict):
        try:
            await websocket.send_json(data)
        except Exception:
            pass

    # We use a task group to manage multiple subscriptions if needed, 
    # but for simplicity, we'll listen to a unified 'telemetry' channel 
    # or multiple specific ones.
    
    # For this implementation, we subscribe to the tenant's specific telemetry channel
    channel_name = f"telemetry:{tenant_id}"
    
    try:
        await bus.subscribe(channel_name, _send_to_ws)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.debug("dashboard_ws_error tenant=%s error=%s", tenant_id, e)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass

_DIAGNOSTIC_LOG_COMPONENTS = {
    "worker": "agent_worker.log",
    "agent_worker": "agent_worker.log",
    "orch": "orchestrator.log",
    "orchestrator": "orchestrator.log",
    "scheduler": "scheduler_worker.log",
    "observer": "observer_worker.log",
    "readiness": "readiness_worker.log",
    "bridge": "external_agent_bridge_worker.log",
    "external_bridge": "external_agent_bridge_worker.log",
    "reputation": "reputation_worker.log",
    "entropy": "entropy_worker.log",
    "policy": "policy_worker.log",
    "mutation": "mutation_worker.log",
    "finops": "finops_worker.log",
    "deploy_verify": "deploy_verify_worker.log",
    "pattern_promotion": "pattern_promotion_worker.log",
    "release": "release_worker.log",
    "metrics": "metrics_exporter.log",
    "webhook": "webhook_worker.log",
    "peer_review": "peer_review_agent.log",
}
_DIAGNOSTIC_LOG_STREAM = "diagnostic_logs"
_DIAGNOSTIC_LOG_OFFSET_PREFIX = "sinc:diaglog:offset:"


async def _set_runtime_config(tenant_id: str, key: str, value: Any) -> None:
    redis_client = get_async_redis()
    if redis_client:
        await redis_client.set(f"sinc:config:{tenant_id}:{key}", value)


async def _get_runtime_config(tenant_id: str, key: str, default: Any = None) -> Any:
    redis_client = get_async_redis()
    if redis_client:
        val = await redis_client.get(f"sinc:config:{tenant_id}:{key}")
        if isinstance(val, bytes):
            val = val.decode("utf-8", errors="replace")
        return val if val is not None else default
    return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _score_badge(score: float) -> tuple[str, str]:
    if score >= 0.95:
        return "A+", "var(--gr)"
    if score >= 0.85:
        return "A", "var(--bl)"
    if score >= 0.75:
        return "B+", "var(--am)"
    return "C", "var(--rd)"


def _short_error(exc: Exception) -> str:
    return f"{exc.__class__.__name__}:{exc}"


def _normalize_metadata(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return raw_value
    if isinstance(raw_value, str) and raw_value.strip():
        try:
            parsed = json.loads(raw_value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _diagnostic_logs_dir() -> Path:
    return Path(env_get("LOGS_DIR", default="g:/Fernando/project0/ai-orchestrator/logs"))


def _normalize_diagnostic_components(component: str, components: str) -> list[str]:
    requested = []
    for raw in [component, *str(components or "").split(",")]:
        item = str(raw or "").strip().lower()
        if not item:
            continue
        normalized = "orch" if item in {"orch", "orchestrator"} else item
        if normalized not in requested:
            requested.append(normalized)
    return requested or ["worker"]


def _diagnostic_log_path(component: str, logs_dir: Path) -> Path:
    filename = _DIAGNOSTIC_LOG_COMPONENTS.get(component, f"{component}.log")
    return logs_dir / filename


def _diagnostic_log_offset_key(component: str) -> str:
    return f"{_DIAGNOSTIC_LOG_OFFSET_PREFIX}{component}"


def _diagnostic_log_stream_name(component: str) -> str:
    return f"sinc:stream:{_DIAGNOSTIC_LOG_STREAM}:{component}"


def _build_diagnostic_log_entry(component: str, line: str, source_path: Path) -> dict[str, Any]:
    entry_ts = _extract_log_timestamp(line) or datetime.now(timezone.utc)
    return {
        "component": component,
        "line": str(line or "").rstrip("\r\n"),
        "level": _log_level_from_line(line),
        "fingerprint": _log_fingerprint(line),
        "ts": entry_ts.isoformat(),
        "source_path": str(source_path),
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }


async def _project_diagnostic_logs_once(*, max_lines_per_component: int = 500) -> dict[str, Any]:
    redis_client = get_async_redis()
    if not redis_client:
        return {"ok": False, "reason": "redis_unavailable", "projected": 0}

    bus = await get_event_bus()
    logs_dir = _diagnostic_logs_dir()
    projected = 0

    for component_name in sorted(_DIAGNOSTIC_LOG_COMPONENTS):
        log_file = _diagnostic_log_path(component_name, logs_dir)
        if not log_file.exists():
            continue

        try:
            stat = log_file.stat()
        except Exception:
            continue

        try:
            raw_offset = await redis_client.get(_diagnostic_log_offset_key(component_name))
            offset = int(raw_offset or 0)
        except Exception:
            offset = 0

        if offset > stat.st_size:
            offset = 0

        emitted = 0
        try:
            with log_file.open("r", encoding="utf-8", errors="replace") as handle:
                handle.seek(offset)
                while emitted < max_lines_per_component:
                    line = handle.readline()
                    if not line:
                        break
                    payload = _build_diagnostic_log_entry(component_name, line, log_file)
                    stream_key = _diagnostic_log_stream_name(component_name)
                    # We publish to the component-specific stream.
                    # This ensures thatChatty components don't evict logs from silent ones.
                    await bus.publish(stream_key, payload, use_stream=True)
                    projected += 1
                    emitted += 1
                next_offset = handle.tell()
        except Exception as exc:
            log.debug("diagnostic_log_projection_error component=%s error=%s", component_name, exc)
            continue

        try:
            await redis_client.set(_diagnostic_log_offset_key(component_name), next_offset)
        except Exception as exc:
            log.debug("diagnostic_log_offset_update_error component=%s error=%s", component_name, exc)

    return {"ok": True, "projected": projected}


async def run_diagnostic_log_projection_loop() -> None:
    await asyncio.sleep(5)
    while True:
        try:
            await _project_diagnostic_logs_once()
        except Exception as exc:
            log.debug("diagnostic_log_projection_loop_error error=%s", exc)
        await asyncio.sleep(2)


def _summarize_diagnostic_reports(
    reports: list[dict[str, Any]],
    *,
    requested_components: list[str],
    effective_window: int,
    pattern: str,
) -> dict[str, Any]:
    aggregate_totals = {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0}
    aggregate_lines = 0
    pattern_buckets: dict[str, dict[str, Any]] = {}

    for report in reports:
        if report.get("error"):
            continue
        aggregate_lines += int(report.get("returned") or 0)
        for level_name, count in (report.get("level_counts") or {}).items():
            aggregate_totals[level_name] = aggregate_totals.get(level_name, 0) + int(count or 0)
        for item in report.get("_entries") or []:
            fingerprint = item["fingerprint"]
            bucket = pattern_buckets.setdefault(
                fingerprint,
                {
                    "count": 0,
                    "components": set(),
                    "levels": set(),
                    "last_seen": None,
                },
            )
            bucket["count"] += 1
            bucket["components"].add(item["component"])
            bucket["levels"].add(item["level"])
            ts = item.get("parsed_ts")
            if ts and (bucket["last_seen"] is None or ts > bucket["last_seen"]):
                bucket["last_seen"] = ts

    patterns = []
    anomalies = []
    for key, meta in sorted(pattern_buckets.items(), key=lambda item: item[1]["count"], reverse=True)[:10]:
        serialized = {
            "pattern": key,
            "count": meta["count"],
            "components": sorted(meta["components"]),
            "levels": sorted(meta["levels"]),
            "last_seen": meta["last_seen"].isoformat() if meta["last_seen"] else None,
        }
        patterns.append(serialized)
        if meta["count"] >= 3 and "ERROR" in meta["levels"]:
            anomalies.append({"type": "repeated_error_pattern", **serialized})
        elif len(meta["components"]) > 1 and "ERROR" in meta["levels"]:
            anomalies.append({"type": "cross_component_error_pattern", **serialized})
        elif meta["count"] >= 4 and "WARN" in meta["levels"] and "ERROR" not in meta["levels"]:
            anomalies.append({"type": "warning_burst", **serialized})

    recommendations = []
    if anomalies:
        recommendations.append(
            "Inspect the highest-count anomaly first; repeated fingerprints indicate a systemic fault instead of isolated noise."
        )
    if aggregate_totals.get("ERROR", 0) and not aggregate_totals.get("WARN", 0):
        recommendations.append(
            "The error-only profile suggests abrupt failure without graceful retry handling."
        )
    if not reports:
        recommendations.append(
            "No component logs were available; confirm LOGS_DIR, Redis, and projection loop availability."
        )

    cleaned_reports = []
    for report in reports:
        item = dict(report)
        item.pop("_entries", None)
        cleaned_reports.append(item)

    if len(requested_components) == 1 and cleaned_reports:
        report = cleaned_reports[0]
        if report.get("error"):
            return report
        return {
            "component": report["component"],
            "path": report["path"],
            "lines": report["lines"],
            "returned": report["returned"],
            "total_available": report["total_available"],
            "window_minutes": report["window_minutes"],
            "pattern": report["pattern"],
            "level_counts": report["level_counts"],
            "skipped_without_ts": report["skipped_without_ts"],
            "patterns": patterns,
            "anomalies": anomalies,
            "recommendations": recommendations,
        }

    return {
        "components_requested": requested_components,
        "components": cleaned_reports,
        "totals": aggregate_totals,
        "returned": aggregate_lines,
        "window_minutes": effective_window,
        "pattern": pattern,
        "patterns": patterns,
        "anomalies": anomalies,
        "recommendations": recommendations,
    }


async def _query_diagnostic_log_stream(
    *,
    requested_components: list[str],
    effective_window: int,
    pattern: str,
    limit: int,
) -> dict[str, Any] | None:
    redis_client = get_async_redis()
    if not redis_client:
        return None

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=effective_window)
        if effective_window > 0
        else None
    )
    per_component: dict[str, list[dict[str, Any]]] = {name: [] for name in requested_components}

    for component_name in requested_components:
        try:
            # Query the specific stream for this component.
            # This is 100% accurate and doesn't depend on other components' chatters.
            raw_entries = await redis_client.xrevrange(
                _diagnostic_log_stream_name(component_name),
                count=limit * 2,  # Buffer for pattern/cutoff filtering
            )
        except Exception as exc:
            log.debug("diagnostic_log_stream_query_error component=%s error=%s", component_name, exc)
            continue

        for _msg_id, data in raw_entries:
            raw_payload = data.get("data", "{}")
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                continue
            payload_component = str(payload.get("component") or "").strip().lower()
            # Verification: ensure payload matches the component (defensive)
            if payload_component != component_name:
                continue

            parsed_ts = _extract_log_timestamp(str(payload.get("line") or "")) or _extract_log_timestamp(str(payload.get("ts") or ""))
            if cutoff and parsed_ts and parsed_ts < cutoff:
                continue
            if pattern and pattern.lower() not in str(payload.get("line") or "").lower():
                continue
            per_component[component_name].append(
                {
                    "component": component_name,
                    "line": str(payload.get("line") or ""),
                    "level": str(payload.get("level") or _log_level_from_line(str(payload.get("line") or ""))),
                    "fingerprint": str(payload.get("fingerprint") or _log_fingerprint(str(payload.get("line") or ""))),
                    "parsed_ts": parsed_ts,
                    "source_path": str(payload.get("source_path") or ""),
                }
            )

    reports = []
    for component_name in requested_components:
        entries = per_component.get(component_name) or []
        if not entries:
            reports.append(
                {
                    "component": component_name,
                    "path": str(_diagnostic_log_path(component_name, _diagnostic_logs_dir())),
                    "lines": [],
                    "returned": 0,
                    "total_available": 0,
                    "window_minutes": effective_window,
                    "pattern": pattern,
                    "level_counts": {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0},
                    "skipped_without_ts": 0,
                    "_entries": [],
                }
            )
            continue

        trimmed = entries[:limit]
        level_counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0}
        for item in entries:
            level_counts[item["level"]] = level_counts.get(item["level"], 0) + 1
        reports.append(
            {
                "component": component_name,
                "path": trimmed[0]["source_path"] or str(_diagnostic_log_path(component_name, _diagnostic_logs_dir())),
                "lines": [item["line"] for item in trimmed],
                "returned": len(trimmed),
                "total_available": len(entries),
                "window_minutes": effective_window,
                "pattern": pattern,
                "level_counts": level_counts,
                "skipped_without_ts": 0,
                "_entries": entries,
            }
        )

    if not any(report.get("returned") for report in reports):
        return None
    return _summarize_diagnostic_reports(
        reports,
        requested_components=requested_components,
        effective_window=effective_window,
        pattern=pattern,
    )


def _normalize_probe_status(raw_status: Any, *, optional: bool = False) -> tuple[str, str]:
    detail = str(raw_status or "unknown")
    normalized = detail.lower()
    if normalized in {"ok", "up", "configured", "connected"} or normalized.startswith("ok-"):
        return "up", detail
    if normalized in {"not_configured", "unavailable", "driver_missing", "unknown"}:
        return ("warn" if optional else "err"), detail
    if normalized == "disconnected":
        return ("warn" if optional else "err"), detail
    if normalized in {"limited", "degraded"}:
        return "warn", detail
    if normalized.startswith("timeout") or normalized.startswith("error"):
        return "err", detail
    return ("warn" if optional else "err"), detail


def _diagnostic_component_payload(
    *,
    status: str,
    detail: str,
    latency_ms: Any = None,
    optional: bool = False,
    **extra: Any,
) -> dict[str, Any]:
    normalized_status, normalized_detail = _normalize_probe_status(status, optional=optional)
    payload = {
        "status": normalized_status,
        "raw_status": str(status or "unknown"),
        "detail": detail or normalized_detail,
        "latency_ms": latency_ms,
    }
    payload.update(extra)
    return payload


async def _table_exists(cur, table_name: str) -> bool:
    await cur.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (table_name,))
    row = await cur.fetchone()
    return bool(row and row["present"])


async def _table_has_tenant(cur, table_name: str) -> bool:
    return "tenant_id" in await get_table_columns_cached(cur, table_name)


async def _get_heartbeat_time_column(cur) -> str:
    cols = await get_table_columns_cached(cur, "heartbeats")
    if "beat_at" in cols:
        return "beat_at"
    return "updated_at"


async def _fetch_qdrant_point_count(tenant_id: str) -> dict[str, Any]:
    host = str(env_get("QDRANT_HOST", default="")).strip()
    port = str(env_get("QDRANT_PORT", default="6333")).strip()
    if not host:
        return {"value": None, "status": "not_configured"}

    try:
        async with create_resilient_client(
            service_name="dashboard-api",
            timeout=2.0,
        ) as client:
            response = await client.get(f"http://{host}:{port}/collections")
            response.raise_for_status()
            collections = response.json().get("result", {}).get("collections", [])
            tenant_prefix = f"{tenant_id}_"
            tenant_collections = [
                item.get("name")
                for item in collections
                if str(item.get("name", "")).startswith(tenant_prefix)
            ]
            total_points = 0
            for collection_name in tenant_collections:
                details = await client.get(
                    f"http://{host}:{port}/collections/{collection_name}"
                )
                details.raise_for_status()
                result = details.json().get("result", {})
                points = result.get("points_count")
                if points is None:
                    points = result.get("vectors_count")
                if points is None:
                    points = result.get("indexed_vectors_count")
                total_points += _coerce_int(points, 0)
        status = "ok" if tenant_collections else "ok-no-tenant-collections"
        return {
            "value": total_points,
            "status": status,
            "collections": len(tenant_collections),
        }
    except Exception as exc:
        return {"value": None, "status": f"error:{_short_error(exc)}"}


async def _fetch_neo4j_node_count(tenant_id: str) -> dict[str, Any]:
    uri = str(env_get("NEO4J_URI", default="")).strip()
    if not uri:
        return {"value": None, "status": "not_configured"}

    user = (
        env_get("NEO4J_USER")
        or env_get("NEO4J_USERNAME")
        or "neo4j"
    )
    password = (
        env_get("NEO4J_PASSWORD")
        or env_get("NEO4J_PASS")
        or env_get("NEO4J_AUTH", default="neo4j/neo4j").split("/", 1)[-1]
    )

    def _query() -> dict[str, Any]:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(uri, auth=(user, password))
        try:
            with driver.session() as session:
                tenant_row = session.run(
                    """
                    MATCH (n)
                    WHERE n.tenant_id = $tenant_id
                    RETURN count(n) AS total
                    """,
                    tenant_id=tenant_id,
                ).single()
                tenant_total = _coerce_int(tenant_row["total"] if tenant_row else 0, 0)
                if tenant_total > 0:
                    return {"value": tenant_total, "status": "ok"}

                global_row = session.run(
                    "MATCH (n) RETURN count(n) AS total"
                ).single()
                return {
                    "value": _coerce_int(global_row["total"] if global_row else 0, 0),
                    "status": "ok-global",
                }
        finally:
            driver.close()

    try:
        return await asyncio.to_thread(_query)
    except ImportError:
        return {"value": None, "status": "driver_missing"}
    except Exception as exc:
        return {"value": None, "status": f"error:{_short_error(exc)}"}


async def _agent_reputation_exprs(cur) -> dict[str, str]:
    cols = await get_table_columns_cached(cur, "agent_reputation")
    return {
        "score": (
            "COALESCE(semantic_score, reputation_fit_score, runtime_success_rate, 0.0)"
            if "semantic_score" in cols and "reputation_fit_score" in cols
            else "COALESCE(semantic_score, runtime_success_rate, 0.0)"
            if "semantic_score" in cols
            else "COALESCE(reputation_fit_score, runtime_success_rate, 0.0)"
            if "reputation_fit_score" in cols
            else "COALESCE(runtime_success_rate, 0.0)"
            if "runtime_success_rate" in cols
            else "0.0"
        ),
        "runtime_success_rate": (
            "COALESCE(runtime_success_rate, 0.0)"
            if "runtime_success_rate" in cols
            else "0.0"
        ),
        "tasks_total": "COALESCE(tasks_total, 0)" if "tasks_total" in cols else "0",
    }


@router.get("/intelligence/memory-stats")
async def get_memory_stats(tenant_id: str = Query(default="default")):
    """
    Returns counts for each memory layer.

    No mock values are emitted. When a layer is unavailable, the count is null
    and the response includes a companion status field.
    """
    redis_client = get_async_redis()
    redis_count = 0
    redis_status = "ok"
    if redis_client:
        try:
            redis_count = len(await redis_client.keys(f"l0:{tenant_id}:*"))
        except Exception as exc:
            redis_status = f"error:{_short_error(exc)}"
    else:
        redis_status = "unavailable"

    qdrant_stats, neo4j_stats = await asyncio.gather(
        _fetch_qdrant_point_count(tenant_id),
        _fetch_neo4j_node_count(tenant_id),
    )

    postgres_count = 0
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            task_count = 0
            goal_count = 0
            if await _table_exists(cur, "tasks"):
                tasks_has_tenant = await _table_has_tenant(cur, "tasks")
                await cur.execute(
                    "SELECT COUNT(*) AS count FROM tasks"
                    + (" WHERE tenant_id = %s" if tasks_has_tenant else ""),
                    (tenant_id,) if tasks_has_tenant else (),
                )
                row = await cur.fetchone()
                task_count = _coerce_int(row["count"] if row else 0, 0)
            if await _table_exists(cur, "goals"):
                goals_has_tenant = await _table_has_tenant(cur, "goals")
                await cur.execute(
                    "SELECT COUNT(*) AS count FROM goals"
                    + (" WHERE tenant_id = %s" if goals_has_tenant else ""),
                    (tenant_id,) if goals_has_tenant else (),
                )
                row = await cur.fetchone()
                goal_count = _coerce_int(row["count"] if row else 0, 0)
            postgres_count = task_count + goal_count

    return {
        "l1_redis": redis_count,
        "l1_redis_status": redis_status,
        "l2_qdrant": qdrant_stats["value"],
        "l2_qdrant_status": qdrant_stats["status"],
        "l3_neo4j": neo4j_stats["value"],
        "l3_neo4j_status": neo4j_stats["status"],
        "l4_postgres": postgres_count,
    }


@router.get("/intelligence/agent-details/{agent_id}")
async def get_agent_details(agent_id: str, tenant_id: str = Query(default="default")):
    """Deep inspection of a worker using tenant-scoped DB state."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            if not await _table_exists(cur, "agent_reputation"):
                raise HTTPException(status_code=503, detail="agent_reputation table unavailable")

            rep_exprs = await _agent_reputation_exprs(cur)
            await cur.execute(
                """
                SELECT agent_name,
                       {tasks_total} AS tasks_total,
                       {runtime_success_rate} AS runtime_success_rate,
                       {score} AS semantic_score,
                       updated_at
                  FROM agent_reputation
                 WHERE agent_name = %s
                   AND tenant_id = %s
                """.format(**rep_exprs),
                (agent_id, tenant_id),
            )
            reputation = await cur.fetchone()
            if not reputation:
                raise HTTPException(status_code=404, detail="Agent not found")

            latest_heartbeat = None
            if await _table_exists(cur, "heartbeats"):
                heartbeats_has_tenant = await _table_has_tenant(cur, "heartbeats")
                heartbeat_time_col = await _get_heartbeat_time_column(cur)
                await cur.execute(
                    """
                    SELECT task_id, {heartbeat_time_col} AS beat_at, progress_pct, current_step, metadata
                      FROM heartbeats
                     WHERE agent_name = %s
                       {heartbeat_scope}
                     ORDER BY {heartbeat_time_col} DESC
                     LIMIT 1
                    """.format(
                        heartbeat_time_col=heartbeat_time_col,
                        heartbeat_scope="AND tenant_id = %s" if heartbeats_has_tenant else "",
                    ),
                    (agent_id, tenant_id) if heartbeats_has_tenant else (agent_id,),
                )
                latest_heartbeat = await cur.fetchone()

    metadata = _normalize_metadata(
        latest_heartbeat["metadata"] if latest_heartbeat else {}
    )
    return {
        "id": agent_id,
        "status": "active" if latest_heartbeat else "idle",
        "last_thought": (
            latest_heartbeat["current_step"] if latest_heartbeat else "No live heartbeat"
        ),
        "context_size": metadata.get("context_size_kb"),
        "memory_usage": metadata.get("memory_usage_mb"),
        "uptime": metadata.get("uptime"),
        "task_id": latest_heartbeat["task_id"] if latest_heartbeat else None,
        "progress_pct": latest_heartbeat["progress_pct"] if latest_heartbeat else None,
        "runtime_success_rate": _coerce_float(reputation["runtime_success_rate"], 0.0),
        "semantic_score": _coerce_float(reputation["semantic_score"], 0.0),
        "tasks_total": _coerce_int(reputation["tasks_total"], 0),
    }


async def _fetch_reputation(cur, tenant_id: str) -> list[dict[str, Any]]:
    if not await _table_exists(cur, "agent_reputation"):
        return []

    rep_exprs = await _agent_reputation_exprs(cur)
    await cur.execute(
        """
        SELECT agent_name,
               {score} AS score,
               {runtime_success_rate} AS runtime_success_rate,
               {tasks_total} AS tasks_total
          FROM agent_reputation
         WHERE tenant_id = %s
         ORDER BY score DESC NULLS LAST, tasks_total DESC
         LIMIT 5
        """.format(**rep_exprs),
        (tenant_id,),
    )
    rows = await cur.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        score = _coerce_float(row["score"], 0.0)
        badge, color = _score_badge(score)
        items.append(
            {
                "name": row["agent_name"],
                "score": round(score * 100, 1),
                "badge": badge,
                "color": color,
                "runtime_success_rate": _coerce_float(
                    row["runtime_success_rate"], 0.0
                ),
                "tasks_total": _coerce_int(row["tasks_total"], 0),
            }
        )
    return items


async def _fetch_agent_fleet(cur, tenant_id: str) -> list[dict[str, Any]]:
    if not await _table_exists(cur, "heartbeats"):
        return []
    has_tasks_table = await _table_exists(cur, "tasks")
    task_pk = await get_task_pk_column(cur) if has_tasks_table else "id"
    tasks_has_tenant = await _table_has_tenant(cur, "tasks") if has_tasks_table else False
    heartbeats_has_tenant = await _table_has_tenant(cur, "heartbeats")
    heartbeat_time_col = await _get_heartbeat_time_column(cur)

    if has_tasks_table:
        await cur.execute(
            """
            WITH latest AS (
                SELECT DISTINCT ON (h.agent_name)
                       h.agent_name,
                       h.task_id,
                       h.{heartbeat_time_col} AS beat_at,
                       h.progress_pct,
                       h.current_step,
                       h.metadata
                  FROM heartbeats h
                 WHERE {heartbeat_scope}
                 ORDER BY h.agent_name, h.{heartbeat_time_col} DESC
            )
            SELECT latest.agent_name,
                   latest.task_id,
                   latest.beat_at,
                   latest.progress_pct,
                   latest.current_step,
                   latest.metadata,
                   t.title AS task_title,
                   t.status AS task_status
              FROM latest
              LEFT JOIN tasks t
                ON t.{task_pk} = latest.task_id
               {task_scope}
             ORDER BY latest.beat_at DESC
             LIMIT 12
            """.format(
                heartbeat_scope="h.tenant_id = %s" if heartbeats_has_tenant else "TRUE",
                heartbeat_time_col=heartbeat_time_col,
                task_pk=task_pk,
                task_scope="AND t.tenant_id = %s" if tasks_has_tenant else "",
            ),
            (tenant_id, tenant_id) if heartbeats_has_tenant and tasks_has_tenant else (tenant_id,) if heartbeats_has_tenant or tasks_has_tenant else (),
        )
    else:
        await cur.execute(
            """
            SELECT DISTINCT ON (h.agent_name)
                   h.agent_name,
                   h.task_id,
                   h.{heartbeat_time_col} AS beat_at,
                   h.progress_pct,
                   h.current_step,
                   h.metadata,
                   NULL::text AS task_title,
                   NULL::text AS task_status
              FROM heartbeats h
             WHERE {heartbeat_scope}
             ORDER BY h.agent_name, h.{heartbeat_time_col} DESC
             LIMIT 12
            """.format(
                heartbeat_scope="h.tenant_id = %s" if heartbeats_has_tenant else "TRUE",
                heartbeat_time_col=heartbeat_time_col,
            ),
            (tenant_id,) if heartbeats_has_tenant else (),
        )
    rows = await cur.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        metadata = _normalize_metadata(row["metadata"])
        items.append(
            {
                "name": row["agent_name"],
                "status": (
                    "online"
                    if row["beat_at"] is not None
                    else "idle"
                ),
                "task_id": row["task_id"],
                "task_title": row["task_title"],
                "task_status": row["task_status"],
                "current_step": row["current_step"] or "Waiting for next step",
                "progress_pct": _coerce_int(row["progress_pct"], 0),
                "context_size_kb": metadata.get("context_size_kb"),
                "memory_usage_mb": metadata.get("memory_usage_mb"),
                "uptime": metadata.get("uptime"),
            }
        )
    return items


async def _fetch_pipeline(cur, tenant_id: str) -> list[dict[str, Any]]:
    if not await _table_exists(cur, "tasks"):
        return []
    task_pk = await get_task_pk_column(cur)
    tasks_has_tenant = await _table_has_tenant(cur, "tasks")

    await cur.execute(
        """
        SELECT {task_pk} AS id, title, status, priority, assigned_agent
          FROM tasks
         {task_scope}
         ORDER BY created_at DESC
         LIMIT 5
        """.format(
            task_pk=task_pk,
            task_scope="WHERE tenant_id = %s" if tasks_has_tenant else "",
        ),
        (tenant_id,) if tasks_has_tenant else (),
    )
    rows = await cur.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        status = row["status"]
        items.append(
            {
                "id": row["id"],
                "name": row["title"],
                "sub": f"{row['assigned_agent'] or 'auto'} · P{row['priority']}",
                "prog": 100 if status == "done" else (45 if status == "in-progress" else 0),
                "status": "run" if status == "in-progress" else ("done" if status == "done" else "sched"),
                "c": "var(--gr)" if status == "done" else "var(--bl)",
            }
        )
    return items


async def _fetch_simulations(cur, tenant_id: str) -> list[dict[str, Any]]:
    if not await _table_exists(cur, "simulation_evaluations"):
        return []

    await cur.execute(
        """
        SELECT task_id, predicted_success, strategy_name, error_delta, created_at
          FROM simulation_evaluations
         WHERE tenant_id = %s
         ORDER BY created_at DESC
         LIMIT 5
        """,
        (tenant_id,),
    )
    rows = await cur.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        strategy = row["strategy_name"] or "unknown"
        items.append(
            {
                "task": str(row["task_id"])[:8],
                "strategy": strategy,
                "success": f"{_coerce_float(row['predicted_success'], 0.0) * 100:.0f}%",
                "uplift": "+15%" if "experimental" in strategy else "std",
                "c": "var(--pu)" if "experimental" in strategy else "var(--bl)",
            }
        )
    return items


async def _fetch_adaptations(cur, tenant_id: str) -> list[dict[str, Any]]:
    if not await _table_exists(cur, "goal_adaptations"):
        return []

    await cur.execute(
        """
        SELECT goal_id, adaptation_type, reason, created_at
          FROM goal_adaptations
         WHERE tenant_id = %s
         ORDER BY created_at DESC
         LIMIT 5
        """,
        (tenant_id,),
    )
    rows = await cur.fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        reason = str(row["reason"] or "")
        items.append(
            {
                "type": row["adaptation_type"],
                "reason": reason[:60] + ("..." if len(reason) > 60 else ""),
                "ts": row["created_at"].strftime("%H:%M") if row["created_at"] else "",
            }
        )
    return items


async def _fetch_task_debugger(cur, tenant_id: str, task_id: str) -> dict[str, Any] | None:
    if not await _table_exists(cur, "tasks"):
        return None

    task_pk = await get_task_pk_column(cur)
    task_cols = await get_table_columns_cached(cur, "tasks")
    task_scope = "AND tenant_id = %s" if "tenant_id" in task_cols else ""
    await cur.execute(
        f"SELECT * FROM tasks WHERE {task_pk} = %s {task_scope} LIMIT 1",
        (task_id, tenant_id) if task_scope else (task_id,),
    )
    task_row = await cur.fetchone()
    if not task_row:
        return None
    task = dict(task_row)
    metadata = _normalize_metadata(task.get("metadata"))

    dependencies: list[str] = []
    if await _table_exists(cur, "dependencies"):
        dep_col = "dependency_id" if "dependency_id" in await get_table_columns_cached(cur, "dependencies") else "depends_on"
        await cur.execute(
            f"SELECT {dep_col} AS dependency_id FROM dependencies WHERE task_id = %s",
            (task_id,),
        )
        dependencies = [str(row["dependency_id"]) for row in await cur.fetchall() if row.get("dependency_id")]

    latest_dispatch = None
    if await _table_exists(cur, "webhook_dispatches"):
        dispatch_cols = await get_table_columns_cached(cur, "webhook_dispatches")
        dispatch_scope = "AND tenant_id = %s" if "tenant_id" in dispatch_cols else ""
        await cur.execute(
            f"""
            SELECT *
            FROM webhook_dispatches
            WHERE task_id = %s {dispatch_scope}
            ORDER BY COALESCE(completed_at, delivered_at, dispatched_at) DESC NULLS LAST, id DESC
            LIMIT 1
            """,
            (task_id, tenant_id) if "tenant_id" in dispatch_cols else (task_id,),
        )
        latest_dispatch = await cur.fetchone()

    latest_heartbeat = None
    if await _table_exists(cur, "heartbeats"):
        heartbeat_time_col = await _get_heartbeat_time_column(cur)
        heartbeat_cols = await get_table_columns_cached(cur, "heartbeats")
        heartbeat_scope = "AND tenant_id = %s" if "tenant_id" in heartbeat_cols else ""
        await cur.execute(
            f"""
            SELECT task_id, agent_name, {heartbeat_time_col} AS beat_at, progress_pct, current_step, metadata
            FROM heartbeats
            WHERE task_id = %s {heartbeat_scope}
            ORDER BY {heartbeat_time_col} DESC
            LIMIT 1
            """,
            (task_id, tenant_id) if "tenant_id" in heartbeat_cols else (task_id,),
        )
        latest_heartbeat = await cur.fetchone()

    incidents: list[dict[str, Any]] = []
    if await _table_exists(cur, "incidents"):
        await cur.execute(
            """
            SELECT category, severity, status, summary, occurred_at, resolved_at
            FROM incidents
            WHERE tenant_id = %s AND task_id = %s
            ORDER BY occurred_at DESC
            LIMIT 10
            """,
            (tenant_id, task_id),
        )
        incidents = [dict(row) for row in await cur.fetchall()]

    autonomous_actions: list[dict[str, Any]] = []
    if await _table_exists(cur, "task_autonomous_actions"):
        await cur.execute(
            """
            SELECT action_type, reasoning, impact, created_at
            FROM task_autonomous_actions
            WHERE tenant_id = %s AND task_id = %s
            ORDER BY created_at DESC
            LIMIT 20
            """,
            (tenant_id, task_id),
        )
        autonomous_actions = [dict(row) for row in await cur.fetchall()]

    timeline: list[dict[str, Any]] = []
    timeline_rows = []
    timeline_table = None
    if await _table_exists(cur, "mv_task_timeline"):
        timeline_table = "mv_task_timeline"
    elif await _table_exists(cur, "agent_events"):
        timeline_table = "agent_events"

    if timeline_table:
        timeline_cols = await get_table_columns_cached(cur, timeline_table)
        actor_col = (
            "actor"
            if "actor" in timeline_cols
            else "agent_name"
            if "agent_name" in timeline_cols
            else "NULL::text"
        )
        payload_col = "payload" if "payload" in timeline_cols else "NULL::jsonb"
        timeline_scope = "AND tenant_id = %s" if "tenant_id" in timeline_cols else ""
        await cur.execute(
            f"""
            SELECT created_at, event_type, {actor_col} AS actor, {payload_col} AS payload
            FROM {timeline_table}
            WHERE task_id = %s {timeline_scope}
            ORDER BY created_at DESC
            LIMIT 40
            """,
            (task_id, tenant_id) if timeline_scope else (task_id,),
        )
        timeline_rows = await cur.fetchall()

    tool_calls: list[str] = []
    diff_snippets: list[str] = []
    for row in timeline_rows:
        payload = _normalize_metadata(row.get("payload"))
        detail_parts = []
        if row.get("actor"):
            detail_parts.append(str(row["actor"]))
        summary = payload.get("summary") or payload.get("reason") or payload.get("status") or ""
        if summary:
            detail_parts.append(str(summary))
        timeline.append(
            {
                "timestamp": row.get("created_at"),
                "event": row.get("event_type"),
                "detail": " — ".join(detail_parts).strip(" —"),
            }
        )
        for key in ("tool", "tool_name"):
            if payload.get(key):
                tool_calls.append(str(payload[key]))
        for key in ("files", "files_modified"):
            value = payload.get(key)
            if isinstance(value, list):
                diff_snippets.extend(str(item) for item in value if item)

    prompt = {"objective": task.get("description") or task.get("title") or ""}
    llm_calls = 0
    tokens_used = 0
    avg_latency_ms = None
    if await _table_exists(cur, "mv_llm_lineage"):
        await cur.execute(
            """
            SELECT prompt, response, latency_ms, input_tokens, output_tokens
            FROM mv_llm_lineage
            WHERE task_id = %s AND tenant_id = %s
            ORDER BY sequence_no DESC NULLS LAST, event_id DESC
            LIMIT 5
            """,
            (task_id, tenant_id),
        )
        llm_rows = await cur.fetchall()
        if llm_rows:
            latest_llm = llm_rows[0]
            prompt["thought"] = str(latest_llm.get("response") or "")[:400]
            prompt["prompt"] = str(latest_llm.get("prompt") or "")[:2000]
            llm_calls = len(llm_rows)
            latencies = [_coerce_float(row.get("latency_ms"), 0.0) for row in llm_rows]
            avg_latency_ms = round(sum(latencies) / len(latencies), 1) if latencies else None
            for row in llm_rows:
                tokens_used += _coerce_int(row.get("input_tokens"), 0) + _coerce_int(row.get("output_tokens"), 0)

    dispatch_payload = _normalize_metadata(latest_dispatch.get("dispatch_payload") if latest_dispatch else {})
    completion_payload = _normalize_metadata(latest_dispatch.get("completion_payload") if latest_dispatch else {})
    heartbeat_metadata = _normalize_metadata(latest_heartbeat.get("metadata") if latest_heartbeat else {})

    return {
        "id": task_id,
        "metadata": {
            "task_id": task_id,
            "title": task.get("title"),
            "status": task.get("status"),
            "priority": task.get("priority"),
            "assigned_agent": task.get("assigned_agent"),
            "execution_mode": metadata.get("execution_mode", dispatch_payload.get("execution_mode")),
            "runtime_engine": metadata.get("runtime_engine", dispatch_payload.get("runtime_engine")),
            "plan_id": task.get("plan_id") or "",
            "goal_id": str(task.get("goal_id") or metadata.get("goal_id") or ""),
            "created_at": task.get("created_at"),
            "started_at": task.get("started_at"),
            "completed_at": task.get("completed_at"),
            "requires_review": bool(task.get("requires_review", False)),
            "reviewed_by": task.get("reviewed_by"),
            "review_feedback": task.get("review_feedback"),
            "red_team_enabled": bool(task.get("red_team_enabled", False)),
        },
        "context": {
            "reason": metadata.get("reason") or completion_payload.get("summary") or "",
            "execution_profile_reason": metadata.get("goal_execution_mode") or metadata.get("execution_mode") or "",
            "files_affected": metadata.get("files_affected") or [],
            "source_modules": metadata.get("source_modules") or [],
            "dependencies": dependencies,
        },
        "prompt": prompt,
        "tools": sorted({tool for tool in tool_calls if tool}),
        "timeline": timeline,
        "diff": {"snippets": sorted({snippet for snippet in diff_snippets if snippet})},
        "reasoning": {
            "preflight_thought": latest_heartbeat.get("current_step") if latest_heartbeat else "",
            "completion_summary": completion_payload.get("summary") or completion_payload.get("result_summary") or "",
            "autonomous_actions": autonomous_actions,
            "incidents": incidents,
        },
        "resource_usage": {
            "tokens_used": tokens_used,
            "llm_calls": llm_calls,
            "avg_latency_ms": avg_latency_ms,
            "context_size_kb": heartbeat_metadata.get("context_size_kb"),
            "memory_mb": heartbeat_metadata.get("memory_usage_mb"),
            "progress_pct": latest_heartbeat.get("progress_pct") if latest_heartbeat else None,
        },
    }


async def _fetch_summary_metrics(cur, tenant_id: str) -> dict[str, Any]:
    if not await _table_exists(cur, "tasks"):
        return {
            "active_agents": 0,
            "latency_p95": "n/a",
            "tps": 0.0,
        }

    await cur.execute(
        """
        SELECT
            COUNT(*) FILTER (
                WHERE completed_at > NOW() - INTERVAL '5 minutes'
            ) AS completed_last_5m,
            percentile_cont(0.95) WITHIN GROUP (
                ORDER BY EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000
            ) FILTER (
                WHERE started_at IS NOT NULL
                  AND completed_at IS NOT NULL
                  AND completed_at > NOW() - INTERVAL '24 hours'
            ) AS latency_p95_ms
          FROM tasks
         WHERE tenant_id = %s
        """,
        (tenant_id,),
    )
    row = await cur.fetchone()

    active_row = {"active_agents": 0}
    if await _table_exists(cur, "heartbeats"):
        heartbeats_has_tenant = await _table_has_tenant(cur, "heartbeats")
        heartbeat_time_col = await _get_heartbeat_time_column(cur)
        await cur.execute(
            """
            SELECT COUNT(DISTINCT agent_name) AS active_agents
              FROM heartbeats
             WHERE {heartbeat_scope}
               AND {heartbeat_time_col} > NOW() - (%s * INTERVAL '1 minute')
            """.format(
                heartbeat_scope="tenant_id = %s" if heartbeats_has_tenant else "TRUE",
                heartbeat_time_col=heartbeat_time_col,
            ),
            (tenant_id, TASK_STALE_TIMEOUT_M) if heartbeats_has_tenant else (TASK_STALE_TIMEOUT_M,),
        )
        active_row = await cur.fetchone()

    latency_ms = row["latency_p95_ms"] if row else None
    throughput = round(_coerce_int(row["completed_last_5m"], 0) / 300.0, 3) if row else 0.0
    return {
        "active_agents": _coerce_int(active_row["active_agents"] if active_row else 0, 0),
        "latency_p95": f"{round(_coerce_float(latency_ms, 0.0), 1)}ms" if latency_ms is not None else "n/a",
        "tps": throughput,
    }


async def _fetch_red_metrics(cur, tenant_id: str) -> dict[str, Any]:
    if not await _table_exists(cur, "tasks"):
        return {
            "labels": [],
            "request_rate": [],
            "error_rate": [],
            "latency": [],
        }

    await cur.execute(
        """
        WITH buckets AS (
            SELECT generate_series(
                date_trunc('day', NOW()) - INTERVAL '6 day',
                date_trunc('day', NOW()),
                INTERVAL '1 day'
            ) AS bucket_start
        ),
        created_counts AS (
            SELECT date_trunc('day', created_at) AS bucket_start,
                   COUNT(*) AS request_rate
              FROM tasks
             WHERE tenant_id = %s
               AND created_at >= NOW() - INTERVAL '7 day'
             GROUP BY 1
        ),
        error_counts AS (
            SELECT date_trunc('day', updated_at) AS bucket_start,
                   COUNT(*) AS error_rate
              FROM tasks
             WHERE tenant_id = %s
               AND status IN ('failed', 'needs-revision', 'cancelled')
               AND updated_at >= NOW() - INTERVAL '7 day'
             GROUP BY 1
        ),
        latency_stats AS (
            SELECT date_trunc('day', completed_at) AS bucket_start,
                   AVG(EXTRACT(EPOCH FROM (completed_at - started_at)) * 1000) AS latency_ms
              FROM tasks
             WHERE tenant_id = %s
               AND started_at IS NOT NULL
               AND completed_at IS NOT NULL
               AND completed_at >= NOW() - INTERVAL '7 day'
             GROUP BY 1
        )
        SELECT to_char(b.bucket_start, 'DD/MM') AS label,
               COALESCE(c.request_rate, 0) AS request_rate,
               COALESCE(e.error_rate, 0) AS error_rate,
               COALESCE(ROUND(l.latency_ms), 0) AS latency
          FROM buckets b
          LEFT JOIN created_counts c ON c.bucket_start = b.bucket_start
          LEFT JOIN error_counts e ON e.bucket_start = b.bucket_start
          LEFT JOIN latency_stats l ON l.bucket_start = b.bucket_start
         ORDER BY b.bucket_start
        """,
        (tenant_id, tenant_id, tenant_id),
    )
    rows = await cur.fetchall()
    return {
        "labels": [row["label"] for row in rows],
        "request_rate": [_coerce_int(row["request_rate"], 0) for row in rows],
        "error_rate": [_coerce_int(row["error_rate"], 0) for row in rows],
        "latency": [_coerce_int(row["latency"], 0) for row in rows],
    }


async def _get_summary_payload(tenant_id: str) -> dict[str, Any]:
    """Helper to collect dashboard state for both REST and WebSocket."""
    redis_client = get_async_redis()
    if not redis_client:
        return {"status": "error", "message": "Redis unavailable"}

    success_rate, autonomy_score, recovery_rate, confidence_cfg, system_mode = await asyncio.gather(
        redis_client.get(f"sinc:metrics:{tenant_id}:success_rate"),
        redis_client.get(f"sinc:metrics:{tenant_id}:autonomy_score"),
        redis_client.get(f"sinc:metrics:{tenant_id}:recovery_rate"),
        _get_runtime_config(tenant_id, "confidence_threshold", 72.0),
        _get_runtime_config(tenant_id, "system_mode", "normal"),
    )

    path_counts = await asyncio.gather(
        redis_client.get(f"sinc:path_counter:{tenant_id}:instant"),
        redis_client.get(f"sinc:path_counter:{tenant_id}:fast"),
        redis_client.get(f"sinc:path_counter:{tenant_id}:standard"),
        redis_client.get(f"sinc:path_counter:{tenant_id}:deep"),
    )

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            reputation = await _fetch_reputation(cur, tenant_id)
            agent_fleet = await _fetch_agent_fleet(cur, tenant_id)
            pipeline = await _fetch_pipeline(cur, tenant_id)
            simulations = await _fetch_simulations(cur, tenant_id)
            adaptations = await _fetch_adaptations(cur, tenant_id)
            summary_metrics = await _fetch_summary_metrics(cur, tenant_id)
            red_metrics = await _fetch_red_metrics(cur, tenant_id)

    from services.cognitive_orchestrator import get_orchestrator
    orch = get_orchestrator()
    config = orch.config
    
    # Check registry health
    registry_health = await orch.registry.check_health()

    return {
        "type": "summary",
        "status": "online",
        "metrics": {
            "success_rate": _coerce_float(success_rate, 0.0),
            "autonomy_score": _coerce_float(autonomy_score, 0.0),
            "recovery_rate": _coerce_float(recovery_rate, 0.0),
            "active_agents": summary_metrics["active_agents"],
            "latency_p95": summary_metrics["latency_p95"],
            "tps": summary_metrics["tps"],
        },
        "registry_health": registry_health,
        "routing": {
            "instant": _coerce_int(path_counts[0], 0),
            "fast": _coerce_int(path_counts[1], 0),
            "standard": _coerce_int(path_counts[2], 0),
            "deep": _coerce_int(path_counts[3], 0),
        },
        "autonomy": {
            "confidence": config.confidence_threshold,
            "mode": config.system_mode,
            "bypass_admission": config.bypass_admission,
        },
        "reputation": reputation,
        "agent_fleet": agent_fleet,
        "pipeline": pipeline,
        "simulations": simulations,
        "adaptations": adaptations,
        "red_metrics": red_metrics,
        "system_metrics": _get_system_metrics(),
        "active_tenants": await _fetch_all_tenants(cur),
        "active_workers": agent_fleet,
    }

def _get_system_metrics() -> dict[str, float]:
    """Capture real hardware telemetry via psutil."""
    try:
        return {
            "cpu": psutil.cpu_percent(),
            "ram": psutil.virtual_memory().percent,
            "disk": psutil.disk_usage('/').percent,
            "gpu": 0.0, # GPU support would require pynvml or similar
        }
    except Exception:
        return {"cpu": 0.0, "ram": 0.0, "disk": 0.0, "gpu": 0.0}

async def _fetch_all_tenants(cur) -> list[dict[str, Any]]:
    """Fetch all tenants from the database to populate the sidebar/tenants list."""
    if not await _table_exists(cur, "tenants"):
        return []
    
    await cur.execute("SELECT id, name, created_at FROM tenants LIMIT 20")
    rows = await cur.fetchall()
    tenants = []
    for row in rows:
        tenants.append({
            "tenant_id": row["id"],
            "name": row["name"] or row["id"],
            "active_agents": 1, # Placeholder logic if not tracked per-tenant in a specific table
            "tokens_today": 0,
            "quota_pct": 10,
        })
    return tenants


async def run_telemetry_broadcaster(tenant_id: str = "default"):
    """
    Background loop that pushes telemetry snapshots to Redis Pub/Sub.
    Enables low-latency updates for all connected WebSockets.
    """
    bus = await get_event_bus()
    channel = f"telemetry:{tenant_id}"
    while True:
        try:
            payload = await _get_summary_payload(tenant_id)
            log.info("TELEMETRY_EMISSION tenant=%s metrics=%s", tenant_id, list(payload.get('metrics', {}).keys()))
            await bus.publish(channel, payload, use_stream=False)
        except Exception as e:
            log.debug("dashboard_broadcaster_error tenant=%s error=%s", tenant_id, e)
        await asyncio.sleep(2)  # High-frequency update for real-time NOC feel


@router.get("/summary")
async def get_dashboard_summary(tenant_id: str = Query(default="default")):
    """Return dashboard metrics backed by real runtime state."""
    return await _get_summary_payload(tenant_id)


@router.get("/task-debugger/{task_id}")
async def get_task_debugger(task_id: str, tenant_id: str = Query(default="default")):
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            payload = await _fetch_task_debugger(cur, tenant_id, task_id)

    if not payload:
        raise HTTPException(status_code=404, detail="Task not found")
    return payload


@router.get("/active-goals")
async def get_active_goals(tenant_id: str = Query(default="default")):
    """List active goals and recent adaptations."""
    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            goals: list[dict[str, Any]] = []
            adaptations: list[dict[str, Any]] = []

            if await _table_exists(cur, "goals"):
                await cur.execute(
                    """
                    SELECT id, title, status, created_at
                      FROM goals
                     WHERE status IN ('pending', 'in-progress')
                       AND tenant_id = %s
                     ORDER BY created_at DESC
                     LIMIT 10
                    """,
                    (tenant_id,),
                )
                goals = await cur.fetchall()

            if await _table_exists(cur, "goal_adaptations"):
                await cur.execute(
                    """
                    SELECT goal_id, adaptation_type, reason, created_at
                      FROM goal_adaptations
                     WHERE tenant_id = %s
                       AND created_at > NOW() - INTERVAL '12 hours'
                     ORDER BY created_at DESC
                     LIMIT 20
                    """,
                    (tenant_id,),
                )
                adaptations = await cur.fetchall()

    return {
        "active_goals": [dict(goal) for goal in goals],
        "recent_adaptations": [dict(adaptation) for adaptation in adaptations],
    }


@router.get("/config")
async def get_dashboard_config(tenant_id: str = Query(default="default")):
    """Read autonomy configuration from real runtime state."""
    orch = get_orchestrator()
    config = orch.config
    return {
        "confidence": config.confidence_threshold,
        "mode": config.system_mode,
        "bypass_admission": config.bypass_admission,
        "ts": env_get("HOSTNAME", default="orchestrator")
    }


@router.post("/config")
async def update_dashboard_config(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Update autonomy configuration in real runtime state."""
    orch = get_orchestrator()
    config = orch.config
    
    if "confidence" in body:
        config.confidence_threshold = float(body["confidence"])
    if "mode" in body:
        config.system_mode = str(body["mode"]).lower()
    if "bypass_admission" in body:
        config.bypass_admission = bool(body["bypass_admission"])
        
    return {
        "ok": True,
        "confidence": config.confidence_threshold,
        "mode": config.system_mode,
        "bypass_admission": config.bypass_admission
    }


@router.get("/feed")
async def get_dashboard_feed(
    kind: str = Query("all"),
    search: str = Query("", max_length=120),
    agent: str = Query("", max_length=80),
    task_id: str = Query("", max_length=160),
    since_hours: int = Query(24, ge=1, le=168),
    before_ts: str = Query("", max_length=64),
    limit: int = Query(50, ge=1, le=100),
    offset: int = 0,
    tenant_id: str = Query(default="default"),
):
    """Historical feed explorer from agent_events."""
    normalized_search = str(search or "").strip().lower()
    normalized_agent = str(agent or "").strip().lower()
    normalized_task_id = str(task_id or "").strip()
    requested_filters = {
        item.strip().lower()
        for item in str(kind or "all").split(",")
        if item.strip()
    }
    if not requested_filters:
        requested_filters = {"all"}
    supported_filters = {"all", "incident", "task", "simulation", "adaptation"}
    requested_filters &= supported_filters
    if not requested_filters:
        requested_filters = {"all"}

    if before_ts:
        try:
            snapshot_bound = datetime.fromisoformat(before_ts.replace("Z", "+00:00"))
            if snapshot_bound.tzinfo is None:
                snapshot_bound = snapshot_bound.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid before_ts: {exc}")
    else:
        snapshot_bound = datetime.now(timezone.utc)
    window_start = snapshot_bound - timedelta(hours=since_hours)

    async with async_db(tenant_id=tenant_id) as conn:
        async with conn.cursor() as cur:
            where_parts = ["tenant_id = %s", "created_at <= %s", "created_at >= %s"]
            params: list[Any] = [tenant_id, snapshot_bound, window_start]
            if normalized_agent:
                where_parts.append("LOWER(COALESCE(actor, '')) = %s")
                params.append(normalized_agent)
            if normalized_task_id:
                where_parts.append("task_id = %s")
                params.append(normalized_task_id)
            await cur.execute(
                f"""
                SELECT id, event_type, actor, payload, created_at, task_id
                  FROM agent_events
                 WHERE {' AND '.join(where_parts)}
                 ORDER BY created_at DESC
                 LIMIT %s OFFSET %s
                """,
                tuple([*params, max(limit * 4, 100), offset]),
            )
            rows = await cur.fetchall()

    def _classify_event(event_type: str) -> str:
        etype = str(event_type or "").lower()
        if any(token in etype for token in ("incident", "error", "fail")):
            return "incident"
        if "sim" in etype:
            return "simulation"
        if "goal" in etype or "adapt" in etype:
            return "adaptation"
        if "task" in etype:
            return "task"
        return "system"

    filtered_rows: list[dict[str, Any]] = []
    for row in rows:
        payload = _normalize_metadata(row["payload"])
        event_kind = _classify_event(row["event_type"])
        if "all" not in requested_filters and event_kind not in requested_filters:
            continue
        haystack = " ".join(
            [
                str(row["event_type"] or ""),
                str(row["actor"] or ""),
                str(row["task_id"] or ""),
                json.dumps(payload, ensure_ascii=False),
            ]
        ).lower()
        if normalized_search and normalized_search not in haystack:
            continue
        filtered_rows.append({"row": row, "event_kind": event_kind, "payload": payload})
        if len(filtered_rows) >= limit:
            break

    items = []
    for entry in filtered_rows:
        row = entry["row"]
        payload = entry["payload"]
        event_kind = entry["event_kind"]
        color = "var(--bl)"
        if event_kind == "incident":
            color = "var(--rd)"
        elif event_kind == "adaptation":
            color = "var(--pu)"
        elif event_kind == "simulation":
            color = "var(--cy)"

        items.append({
            "id": str(row["id"]),
            "kind": event_kind,
            "event_type": str(row["event_type"]),
            "color": color,
            "title": payload.get("title") or payload.get("summary") or row["event_type"],
            "meta": f"{row['actor'] or 'system'} ? {row['created_at'].strftime('%H:%M:%S')}",
            "tag": event_kind.upper(),
            "ts": row["created_at"].isoformat(),
            "task_id": row["task_id"],
        })

    has_more = len(rows) >= max(limit * 4, 100) and len(items) >= limit
    return {
        "items": items,
        "count": len(items),
        "kind": ",".join(sorted(requested_filters)),
        "search": normalized_search,
        "agent": normalized_agent,
        "task_id": normalized_task_id,
        "since_hours": since_hours,
        "snapshot_ts": snapshot_bound.isoformat(),
        "offset": offset,
        "next_offset": offset + len(items),
        "has_more": has_more,
    }


@router.get("/diagnostics/health")
async def get_diagnostic_health(tenant_id: str = Query(default="default")):
    """Expose canonical health diagnostics for dashboard and runner tooling."""
    from services.streaming.core.runtime_plane import compute_readiness_snapshot
    from services.streaming.routes.health import health_deep

    readiness, deep_health = await asyncio.gather(
        compute_readiness_snapshot(tenant_id),
        health_deep(Response()),
    )

    counts = readiness.get("counts") or {}
    cognitive = readiness.get("cognitive") or deep_health.get("cognitive") or {}
    layers = deep_health.get("layers") or {}

    runtime_status = (
        "up"
        if readiness.get("health") == "ok" and readiness.get("status") == "ready"
        else "warn"
        if readiness.get("health") in {"needs-answers", "degraded"}
        else "err"
    )
    cognitive_quality = str(cognitive.get("status") or readiness.get("cognitive_status") or "unknown")
    cognitive_state = (
        "up"
        if cognitive_quality == "full"
        else "warn"
        if cognitive_quality in {"limited", "degraded"}
        else "err"
    )

    components = {
        "runtime": {
            "status": runtime_status,
            "raw_status": str(readiness.get("status") or "unknown"),
            "detail": (
                f"health={readiness.get('health')} "
                f"agents={counts.get('active_agents', 0)} "
                f"incidents={counts.get('open_incidents', 0)} "
                f"pending={counts.get('pending', 0)}"
            ),
            "latency_ms": None,
            "counts": counts,
        },
        "cognitive": {
            "status": cognitive_state,
            "raw_status": cognitive_quality,
            "detail": str(cognitive.get("summary") or "cognitive snapshot available"),
            "latency_ms": None,
            "score": cognitive.get("score"),
            "critical_missing": cognitive.get("critical_missing") or [],
            "optional_missing": cognitive.get("optional_missing") or [],
        },
        "postgres": _diagnostic_component_payload(
            status=layers.get("l1_postgres"),
            detail=f"deep={layers.get('l1_postgres')}",
        ),
        "redis": _diagnostic_component_payload(
            status=layers.get("l0_redis"),
            detail=f"deep={layers.get('l0_redis')}",
            optional=True,
        ),
        "neo4j": _diagnostic_component_payload(
            status=layers.get("l2_neo4j"),
            detail=f"deep={layers.get('l2_neo4j')}",
            optional=True,
        ),
        "qdrant": _diagnostic_component_payload(
            status=layers.get("l3_qdrant"),
            detail=f"deep={layers.get('l3_qdrant')}",
            optional=True,
        ),
        "llm": _diagnostic_component_payload(
            status=layers.get("l4_llm"),
            detail=f"deep={layers.get('l4_llm')}",
            optional=True,
        ),
        "event_bus": _diagnostic_component_payload(
            status=layers.get("event_bus"),
            detail=f"deep={layers.get('event_bus')}",
        ),
        "ollama": _diagnostic_component_payload(
            status=layers.get("ollama"),
            detail=f"deep={layers.get('ollama')}",
            optional=True,
        ),
    }

    issues = []
    if readiness.get("health") != "ok":
        issues.append(f"readiness={readiness.get('health')}")
    if counts.get("open_incidents", 0):
        issues.append(f"open_incidents={counts.get('open_incidents')}")
    if cognitive_quality != "full":
        issues.append(f"cognitive={cognitive_quality}")
    for component_name, payload in components.items():
        if payload.get("status") == "err":
            issues.append(f"{component_name}={payload.get('raw_status')}")

    return {
        "status": deep_health.get("status") or readiness.get("status") or "unknown",
        "health": readiness.get("health") or "unknown",
        "quality": readiness.get("quality") or deep_health.get("quality") or "unknown",
        "components": components,
        "counts": counts,
        "cognitive": cognitive,
        "layers": layers,
        "issues": issues,
        "ts": deep_health.get("ts") or readiness.get("ts"),
    }


@router.get("/diagnostics/logs")
async def get_diagnostic_logs(
    component: str = Query("worker"),
    components: str = Query(""),
    pattern: str = Query("", max_length=160),
    since_minutes: int = Query(0, ge=0, le=10080),
    since_hours: int = Query(0, ge=0, le=168),
    limit: int = Query(100, ge=1, le=1000),
    tenant_id: str = Query(default="default")
):
    """Expose recent logs for one or more components."""
    del tenant_id  # diagnostics are infra-wide, not tenant-scoped file shards

    requested_components = _normalize_diagnostic_components(component, components)
    effective_window = since_minutes or (since_hours * 60)

    # Proactively project a small burst to ensure real-time reactivity
    # The background loop will handle the bulk, but this catches very recent lines.
    await _project_diagnostic_logs_once(max_lines_per_component=max(limit, 50))
    stream_payload = await _query_diagnostic_log_stream(
        requested_components=requested_components,
        effective_window=effective_window,
        pattern=pattern,
        limit=limit,
    )
    if stream_payload is not None:
        return stream_payload

    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=effective_window)
        if effective_window > 0
        else None
    )
    logs_dir = _diagnostic_logs_dir()

    reports = []
    for component_name in requested_components:
        log_file = _diagnostic_log_path(component_name, logs_dir)
        if not log_file.exists():
            reports.append(
                {
                    "component": component_name,
                    "error": "Log file not found",
                    "path": str(log_file),
                    "lines": [],
                    "returned": 0,
                    "total_available": 0,
                    "level_counts": {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0},
                    "_entries": [],
                }
            )
            continue

        try:
            raw_lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            reports.append(
                {
                    "component": component_name,
                    "error": str(exc),
                    "path": str(log_file),
                    "lines": [],
                    "returned": 0,
                    "total_available": 0,
                    "level_counts": {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0},
                    "_entries": [],
                }
            )
            continue

        filtered_lines = []
        level_counts = {"ERROR": 0, "WARN": 0, "INFO": 0, "DEBUG": 0}
        skipped_without_ts = 0
        entries = []
        for line in raw_lines:
            ts = _extract_log_timestamp(line)
            if cutoff and ts and ts < cutoff:
                continue
            if cutoff and ts is None:
                skipped_without_ts += 1
            if pattern and pattern.lower() not in line.lower():
                continue
            filtered_lines.append(line)
            level = _log_level_from_line(line)
            level_counts[level] = level_counts.get(level, 0) + 1
            entries.append(
                {
                    "component": component_name,
                    "line": line,
                    "level": level,
                    "fingerprint": _log_fingerprint(line),
                    "parsed_ts": ts,
                    "source_path": str(log_file),
                }
            )

        trimmed_lines = filtered_lines[-limit:]

        reports.append(
            {
                "component": component_name,
                "path": str(log_file),
                "lines": trimmed_lines,
                "returned": len(trimmed_lines),
                "total_available": len(raw_lines),
                "window_minutes": effective_window,
                "pattern": pattern,
                "level_counts": level_counts,
                "skipped_without_ts": skipped_without_ts,
                "_entries": entries,
            }
        )
    return _summarize_diagnostic_reports(
        reports,
        requested_components=requested_components,
        effective_window=effective_window,
        pattern=pattern,
    )


# ── Ask N5 · Dashboard LLM Chat (no API key required — internal NOC) ─────────

@router.get("/ask")
async def dashboard_ask(
    prompt: str = Query(...),
    project_id: str = Query(default="project0"),
    session_id: str = Query(default=""),
    tenant_id: str = Query(default="default"),
):
    """
    SSE streaming endpoint for the NOC Ask N5 panel.
    Calls Ollama directly with RAG context from ContextRetriever.
    No API key required — internal dashboard use only.
    """
    ollama_host  = _OLLAMA_HOST
    ollama_model = env_get("OLLAMA_MODEL_GENERAL", default="qwen2.5:7b-instruct-q4_K_M")

    # ── Build system prompt + optional RAG context ────────────────────────────
    system_prompt = (
        "You are an expert software engineer assistant for the SINC AI Orchestrator project. "
        "Answer questions about the codebase concisely and precisely. "
        "Reference specific files, functions, and line numbers when relevant. "
        "Use markdown formatting: code blocks, headers, bullet points."
    )
    context_text = ""
    try:
        from services.context_retriever import graph_aware_retrieve
        ctx = await graph_aware_retrieve(prompt, project_id=project_id, tenant_id=tenant_id)
        context_text = ctx.get("context", "") if isinstance(ctx, dict) else ""
    except Exception as _ctx_err:
        log.debug("dashboard_ask_context_error error=%s", _ctx_err)

    if context_text:
        system_prompt += f"\n\nCODEBASE CONTEXT:\n{context_text[:8000]}"

    # ── Load Redis session history ────────────────────────────────────────────
    history: list[dict] = []
    redis_key = f"noc_session:{tenant_id}:{session_id}" if session_id else None
    if redis_key:
        try:
            r = get_async_redis()
            if r:
                raw = await r.get(redis_key)
                if raw:
                    history = json.loads(raw if isinstance(raw, str) else raw.decode())
        except Exception:
            pass

    messages = history + [{"role": "user", "content": prompt}]
    t0 = _time.monotonic()

    # ── SSE generator ─────────────────────────────────────────────────────────
    async def _gen():
        full_answer: list[str] = []
        try:
            async with create_resilient_client(service_name="dashboard-ask", timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{ollama_host}/api/chat",
                    json={
                        "model":    ollama_model,
                        "messages": [{"role": "system", "content": system_prompt}] + messages,
                        "stream":   True,
                    },
                ) as resp:
                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        token = chunk.get("message", {}).get("content", "")
                        if token:
                            full_answer.append(token)
                            yield f"data: {json.dumps({'token': token})}\n\n"
                        if chunk.get("done"):
                            break

            # Persist session ─────────────────────────────────────────────────
            if redis_key:
                try:
                    r = get_async_redis()
                    if r:
                        updated = (history + [
                            {"role": "user",      "content": prompt},
                            {"role": "assistant", "content": "".join(full_answer)},
                        ])[-20:]
                        await r.setex(redis_key, 3600, json.dumps(updated))
                except Exception:
                    pass

            latency_ms = int((_time.monotonic() - t0) * 1000)
            yield f"data: {json.dumps({'done': True, 'latency_ms': latency_ms, 'model': ollama_model, 'session_id': session_id or None})}\n\n"

        except Exception as exc:
            log.warning("dashboard_ask_stream_error error=%s", exc)
            yield f"data: {json.dumps({'error': str(exc)})}\n\n"

    return StreamingResponse(
        _gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )




# ── Agent Reputation (task-history based — no auth) ───────────────────────────

@router.get("/intelligence/reputation")
async def dashboard_reputation(
    tenant_id: str = Query(default="default"),
    limit: int = Query(default=10, le=30),
):
    """Compute agent reputation scores from task success/failure history."""
    rows = []
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if not await _table_exists(cur, "tasks"):
                    return {"agents": []}
                await cur.execute(
                    """
                    SELECT agent_name,
                           COUNT(*)                                                  AS total,
                           COUNT(*) FILTER (WHERE status IN ('done','completed','success')) AS success,
                           COUNT(*) FILTER (WHERE status = 'cancelled')              AS cancelled,
                           AVG(EXTRACT(EPOCH FROM (updated_at - created_at)))        AS avg_duration_s
                      FROM tasks
                     WHERE tenant_id = %s
                       AND agent_name IS NOT NULL
                       AND agent_name != ''
                     GROUP BY agent_name
                     ORDER BY success::float / NULLIF(total, 0) DESC, total DESC
                     LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                rows = await cur.fetchall()
    except Exception as exc:
        log.warning("reputation_error error=%s", exc)
        return {"agents": [], "error": str(exc)}

    agents = []
    for r in rows:
        d = dict(r)
        total   = int(d.get("total", 0) or 0)
        success = int(d.get("success", 0) or 0)
        score   = round((success / total * 100) if total else 0, 1)
        avg_s   = d.get("avg_duration_s")
        agents.append({
            "name":          d["agent_name"],
            "score":         score,
            "total_tasks":   total,
            "success_tasks": success,
            "cancelled":     int(d.get("cancelled", 0) or 0),
            "avg_duration_s": round(float(avg_s), 1) if avg_s else None,
            "badge": "A+" if score >= 95 else "A" if score >= 85 else "B+" if score >= 75 else "B" if score >= 65 else "C+",
        })
    return {"agents": agents}


# ── Confidence Config (no auth — NOC) ─────────────────────────────────────────

@router.post("/confidence")
async def update_confidence(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Persist confidence threshold to Redis so the orchestrator picks it up."""
    value = body.get("value")
    if value is None or not isinstance(value, (int, float)) or not (0 <= value <= 100):
        raise HTTPException(status_code=400, detail="value must be 0-100")
    try:
        r = get_async_redis()
        if r:
            await r.set(f"conf_threshold:{tenant_id}", str(value))
            await r.publish("config_update", json.dumps({"tenant_id": tenant_id, "confidence_threshold": value}))
    except Exception as exc:
        log.warning("confidence_update_error error=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "confidence_threshold": value}


# ── Lessons Learned (no auth — dashboard read-only) ───────────────────────────

@router.get("/intelligence/lessons")
async def dashboard_lessons(
    limit: int = Query(default=20, le=100),
    tenant_id: str = Query(default="default"),
):
    """Fetch recent lessons learned from the database. No API key required."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if not await _table_exists(cur, "lessons_learned"):
                    return {"lessons": [], "count": 0}
                await cur.execute(
                    """
                    SELECT id, error_signature, context, attempted_fix,
                           result, confidence, agent_name, task_id, created_at
                      FROM lessons_learned
                     WHERE tenant_id = %s
                     ORDER BY created_at DESC
                     LIMIT %s
                    """,
                    (tenant_id, limit),
                )
                rows = await cur.fetchall()
        lessons = []
        for i, r in enumerate(rows):
            row = dict(r)
            row["created_at"] = row["created_at"].isoformat() if hasattr(row.get("created_at"), "isoformat") else str(row.get("created_at", ""))
            lessons.append(row)
        return {"lessons": lessons, "count": len(lessons)}
    except Exception as exc:
        log.warning("dashboard_lessons_error error=%s", exc)
        return {"lessons": [], "count": 0, "error": str(exc)}


# ── Worker / Agent Actions (no auth — internal NOC) ───────────────────────────

@router.post("/workers/{agent_id}/action")
async def worker_action(
    agent_id: str,
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """
    Execute an action on a worker/agent: pause | restart | terminate.
    - pause:     marks all pending tasks for this agent as 'paused'
    - restart:   broadcasts restart event; marks paused tasks back to 'pending'
    - terminate: cancels all running/pending tasks for this agent
    """
    action = (body.get("action") or "").lower()
    if action not in ("pause", "restart", "terminate"):
        raise HTTPException(status_code=400, detail="action must be pause | restart | terminate")

    affected = 0
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                if action == "pause":
                    await cur.execute(
                        f"UPDATE tasks SET status = 'paused', updated_at = NOW() "
                        f"WHERE agent_name = %s AND tenant_id = %s AND status IN ('pending','running') "
                        f"RETURNING {task_pk}",
                        (agent_id, tenant_id),
                    )
                elif action == "restart":
                    await cur.execute(
                        f"UPDATE tasks SET status = 'pending', updated_at = NOW() "
                        f"WHERE agent_name = %s AND tenant_id = %s AND status = 'paused' "
                        f"RETURNING {task_pk}",
                        (agent_id, tenant_id),
                    )
                elif action == "terminate":
                    await cur.execute(
                        f"UPDATE tasks SET status = 'cancelled', updated_at = NOW() "
                        f"WHERE agent_name = %s AND tenant_id = %s AND status IN ('pending','running','paused') "
                        f"RETURNING {task_pk}",
                        (agent_id, tenant_id),
                    )
                rows = await cur.fetchall()
                affected = len(rows)
                await conn.commit()
    except Exception as exc:
        log.warning("worker_action_error agent=%s action=%s error=%s", agent_id, action, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    await _write_audit_log(tenant_id, f"worker_{action}", agent_id, f"affected={affected}")
    from services.streaming.core.sse import broadcast
    await broadcast(f"worker_{action}", {"agent_id": agent_id, "affected_tasks": affected}, tenant_id=tenant_id)
    return {"ok": True, "agent_id": agent_id, "action": action, "affected_tasks": affected}


# ── Kill All Running Tasks (no auth — NOC emergency stop) ─────────────────────

@router.post("/tasks/kill-all")
async def kill_all_tasks(
    tenant_id: str = Query(default="default"),
):
    """Cancel ALL running and pending tasks for the tenant. NOC emergency stop."""
    cancelled = 0
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                await cur.execute(
                    f"UPDATE tasks SET status = 'cancelled', updated_at = NOW() "
                    f"WHERE tenant_id = %s AND status IN ('pending','running','paused') "
                    f"RETURNING {task_pk}",
                    (tenant_id,),
                )
                rows = await cur.fetchall()
                cancelled = len(rows)
                await conn.commit()
    except Exception as exc:
        log.warning("kill_all_error error=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    await _write_audit_log(tenant_id, "kill_all_tasks", "all", f"cancelled={cancelled}")
    from services.streaming.core.sse import broadcast
    await broadcast("kill_all", {"cancelled_tasks": cancelled}, tenant_id=tenant_id)
    return {"ok": True, "cancelled_tasks": cancelled}


# ── Dashboard Snapshot (server-side save) ─────────────────────────────────────

@router.post("/snapshot")
async def save_snapshot(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Persist a NOC dashboard snapshot JSON to disk (snapshots/ directory)."""
    import datetime as _dt
    snapshots_dir = Path(env_get("AGENT_WORKSPACE", default=".")) / "snapshots"
    try:
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        ts = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"noc_snapshot_{tenant_id}_{ts}.json"
        filepath = snapshots_dir / filename
        filepath.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
    except Exception as exc:
        log.warning("snapshot_save_error error=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "filename": filename, "path": str(filepath)}


# ── Tenant Provisioning (no auth — NOC) ───────────────────────────────────────

@router.post("/tenants/create")
async def create_tenant_noc(body: dict):
    """
    Provision a new tenant from the NOC dashboard.
    Requires: name (str). Optional: plan (free|pro|enterprise), email (str).
    """
    name = (body.get("name") or "").strip()
    if not name or len(name) < 2:
        raise HTTPException(status_code=400, detail="name must be at least 2 characters")
    plan  = body.get("plan", "free")
    email = body.get("email", "")
    if plan not in ("free", "pro", "enterprise"):
        plan = "free"

    import secrets
    import hashlib
    api_key = "sk-" + secrets.token_hex(24)
    api_key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    tenant_id = name.lower().replace(" ", "_")[:32]

    try:
        async with async_db(tenant_id="default") as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO tenants (tenant_id, name, plan, api_key_hash, email, created_at)
                    VALUES (%s, %s, %s, %s, %s, NOW())
                    ON CONFLICT (tenant_id) DO NOTHING
                    RETURNING tenant_id
                    """,
                    (tenant_id, name, plan, api_key_hash, email),
                )
                row = await cur.fetchone()
                await conn.commit()
    except Exception as exc:
        log.warning("tenant_create_error error=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))

    if not row:
        raise HTTPException(status_code=409, detail=f"Tenant '{tenant_id}' already exists")

    return {
        "ok": True,
        "tenant_id": tenant_id,
        "name": name,
        "plan": plan,
        "api_key": api_key,   # shown once — user must copy
    }


# ── GET /tasks ─────────────────────────────────────────────────────────────────
@router.get("/tasks")
async def list_tasks(
    tenant_id: str = Query(default="default"),
    status: str | None = Query(default=None),
    limit: int = Query(default=40, le=200),
):
    """List tasks for the Prompt Inspector and Tool Timeline pages."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                cols = await get_table_columns_cached(cur, "tasks")
                allowed = {"task_id","id","agent_name","status","prompt","description",
                           "input","tokens_used","created_at","updated_at","tenant_id"}
                select_cols = ", ".join(c for c in cols if c in allowed) or "*"

                if status:
                    await cur.execute(
                        f"SELECT {select_cols} FROM tasks WHERE tenant_id = %s AND status = %s "
                        f"ORDER BY COALESCE(updated_at, created_at) DESC LIMIT %s",
                        (tenant_id, status, limit),
                    )
                else:
                    await cur.execute(
                        f"SELECT {select_cols} FROM tasks WHERE tenant_id = %s "
                        f"ORDER BY COALESCE(updated_at, created_at) DESC LIMIT %s",
                        (tenant_id, limit),
                    )
                rows = await cur.fetchall()
                return rows
    except Exception as exc:
        log.warning("list_tasks_error error=%s", exc)
        return []


# ── POST /tasks/reclaim-zombies ────────────────────────────────────────────────
@router.post("/tasks/reclaim-zombies")
async def reclaim_zombie_tasks(
    tenant_id: str = Query(default="default"),
    stale_minutes: int = Query(default=10),
):
    """Move stale running tasks back to 'pending' (Mass Reclaim)."""
    reclaimed = 0
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                await cur.execute(
                    f"UPDATE tasks SET status = 'pending', updated_at = NOW() "
                    f"WHERE tenant_id = %s AND status = 'running' "
                    f"AND updated_at < NOW() - INTERVAL '{int(stale_minutes)} minutes' "
                    f"RETURNING {task_pk}",
                    (tenant_id,),
                )
                reclaimed = len(await cur.fetchall())
                await conn.commit()
    except Exception as exc:
        log.warning("reclaim_zombies_error error=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    await _write_audit_log(tenant_id, "reclaim_zombies", "tasks", f"reclaimed={reclaimed}")
    return {"ok": True, "reclaimed": reclaimed}


# ── POST /services/{service}/restart ──────────────────────────────────────────
@router.post("/services/{service}/restart")
async def restart_service(
    service: str,
    tenant_id: str = Query(default="default"),
):
    """Attempt to restart a named Docker service via docker CLI."""
    import subprocess
    ALLOWED_SERVICES = {"redis", "qdrant", "neo4j", "postgres", "worker", "ollama"}
    if service not in ALLOWED_SERVICES:
        raise HTTPException(status_code=400, detail=f"Unknown service '{service}'")
    try:
        result = subprocess.run(
            ["docker", "restart", service],
            capture_output=True, text=True, timeout=15
        )
        ok = result.returncode == 0
        return {"ok": ok, "service": service, "output": (result.stdout or result.stderr).strip()}
    except FileNotFoundError:
        return {"ok": False, "service": service, "output": "docker CLI not available"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "service": service, "output": "timeout"}
    except Exception as exc:
        return {"ok": False, "service": service, "output": str(exc)}


# ── POST /tasks/inject ────────────────────────────────────────────────────────
@router.post("/tasks/inject")
async def inject_task(
    payload: dict,
    tenant_id: str = Query(default="default"),
):
    """Manually inject a task into the queue for testing/debugging."""
    import uuid as _uuid
    task_id = payload.get("task_id") or f"manual-{_uuid.uuid4().hex[:8]}"
    agent_name = payload.get("agent_name", "manual")
    prompt = payload.get("prompt") or payload.get("description") or ""
    priority = int(payload.get("priority", 5))
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO tasks (task_id, tenant_id, agent_name, prompt, status, priority, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, 'pending', %s, NOW(), NOW())
                    ON CONFLICT (task_id) DO NOTHING
                    """,
                    (task_id, tenant_id, agent_name, prompt, priority),
                )
                await conn.commit()
    except Exception as exc:
        log.warning("inject_task_error error=%s", exc)
        raise HTTPException(status_code=500, detail=str(exc))
    return {"ok": True, "task_id": task_id, "agent_name": agent_name, "status": "pending"}

# ═══════════════════════════════════════════════════════════════════════════════
# EXTENDED SYSTEM METRICS (GPU temp, VRAM, network, disk IOPS)
# ═══════════════════════════════════════════════════════════════════════════════

# NOTE: Replace the existing /system-metrics endpoint return block with the
# extended version below — see dashboard_api.py:100
# This file contains NEW endpoints to append to the file.


# ── Audit Log helper + endpoints ───────────────────────────────────────────────

async def _write_audit_log(tenant_id: str, action: str, target: str, detail: str = "", actor: str = "noc_dashboard") -> None:
    """Write a timestamped audit entry to Redis sorted set (7-day TTL)."""
    redis_client = get_async_redis()
    if not redis_client:
        return
    import time as _time_module
    ts = _time_module.time()
    entry = json.dumps({"actor": actor, "action": action, "target": target, "detail": detail, "ts": ts})
    key = f"sinc:noc_audit:{tenant_id}"
    try:
        await redis_client.zadd(key, {entry: ts})
        await redis_client.expire(key, 86400 * 7)  # 7-day TTL
    except Exception as exc:
        log.debug("audit_log_write_error error=%s", exc)


@router.get("/audit-log")
async def get_audit_log(
    tenant_id: str = Query(default="default"),
    limit: int = Query(default=50, le=200),
):
    """Return recent NOC audit log entries from Redis."""
    redis_client = get_async_redis()
    if not redis_client:
        return {"entries": [], "source": "redis_unavailable"}
    key = f"sinc:noc_audit:{tenant_id}"
    try:
        raw = await redis_client.zrevrangebyscore(key, "+inf", "-inf", start=0, num=limit)
        entries = []
        for item in raw:
            try:
                if isinstance(item, bytes):
                    item = item.decode("utf-8")
                entries.append(json.loads(item))
            except Exception:
                pass
        return {"entries": entries, "count": len(entries)}
    except Exception as exc:
        log.debug("audit_log_read_error error=%s", exc)
        return {"entries": [], "error": str(exc)}


# ── Agent Control endpoints ────────────────────────────────────────────────────

@router.get("/agents")
async def list_agents(
    tenant_id: str = Query(default="default"),
):
    """Return agent roster with status, reputation, and zombie detection."""
    agents: dict[str, dict] = {}

    # 1. Reputation + task stats from tasks table
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if await _table_exists(cur, "tasks"):
                    await cur.execute(
                        """
                        SELECT agent_name,
                               COUNT(*)                                                            AS total,
                               COUNT(*) FILTER (WHERE status = 'running')                         AS running,
                               COUNT(*) FILTER (WHERE status = 'pending')                         AS pending,
                               COUNT(*) FILTER (WHERE status IN ('done','completed','success'))   AS success,
                               COUNT(*) FILTER (WHERE status = 'failed')                          AS failed,
                               COUNT(*) FILTER (WHERE status = 'running'
                                             AND updated_at < NOW() - INTERVAL '10 minutes')      AS zombie_count,
                               COALESCE(SUM(tokens_used), 0)                                      AS tokens_total,
                               AVG(EXTRACT(EPOCH FROM (updated_at - created_at)))
                                   FILTER (WHERE status IN ('done','completed','success'))         AS avg_duration_s,
                               MAX(updated_at)                                                     AS last_active
                          FROM tasks
                         WHERE tenant_id = %s AND agent_name IS NOT NULL AND agent_name != ''
                         GROUP BY agent_name
                         ORDER BY (COUNT(*) FILTER (WHERE status = 'running')) DESC, MAX(updated_at) DESC
                        """,
                        (tenant_id,),
                    )
                    for row in await cur.fetchall():
                        name = row["agent_name"]
                        total = int(row["total"] or 0)
                        success = int(row["success"] or 0)
                        running = int(row["running"] or 0)
                        zombie_count = int(row["zombie_count"] or 0)
                        rep_score = round(success / total * 100, 1) if total > 0 else 0

                        if zombie_count > 0:
                            status = "zombie"
                        elif running > 0:
                            status = "busy"
                        elif int(row.get("pending") or 0) > 0:
                            status = "queued"
                        else:
                            status = "idle"

                        last_active_raw = row.get("last_active")
                        last_active_str = last_active_raw.isoformat() if hasattr(last_active_raw, "isoformat") else str(last_active_raw or "")

                        agents[name] = {
                            "name": name,
                            "status": status,
                            "total_tasks": total,
                            "running": running,
                            "pending": int(row.get("pending") or 0),
                            "success": success,
                            "failed": int(row.get("failed") or 0),
                            "zombie": zombie_count > 0,
                            "tokens_total": int(row.get("tokens_total") or 0),
                            "avg_duration_s": round(float(row["avg_duration_s"]), 1) if row.get("avg_duration_s") else None,
                            "rep_score": rep_score,
                            "last_active": last_active_str,
                        }
    except Exception as exc:
        log.debug("agents_tasks_query_error error=%s", exc)

    # 2. Live heartbeats (if available)
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                fleet = await _fetch_agent_fleet(cur, tenant_id)
                for item in fleet:
                    name = item.get("agent_name", "")
                    if not name:
                        continue
                    if name not in agents:
                        agents[name] = {"name": name, "status": "idle", "total_tasks": 0, "running": 0,
                                        "pending": 0, "success": 0, "failed": 0, "zombie": False,
                                        "tokens_total": 0, "avg_duration_s": None, "rep_score": 0, "last_active": ""}
                    beat_at = item.get("beat_at")
                    agents[name]["heartbeat"] = beat_at.isoformat() if hasattr(beat_at, "isoformat") else str(beat_at or "")
                    agents[name]["progress_pct"] = item.get("progress_pct")
                    agents[name]["current_step"] = item.get("current_step")
                    agents[name]["task_id"] = item.get("task_id")
                    agents[name]["task_title"] = item.get("task_title") or item.get("current_step") or ""
    except Exception as exc:
        log.debug("agents_heartbeat_query_error error=%s", exc)

    return {"agents": list(agents.values()), "count": len(agents)}


@router.get("/agents/{agent_id}/config")
async def get_agent_config(
    agent_id: str,
    tenant_id: str = Query(default="default"),
):
    """Read per-agent inference config from Redis."""
    temperature = await _get_runtime_config(tenant_id, f"agent:{agent_id}:temperature", default="0.7")
    model       = await _get_runtime_config(tenant_id, f"agent:{agent_id}:model",       default="")
    max_tokens  = await _get_runtime_config(tenant_id, f"agent:{agent_id}:max_tokens",  default="4096")
    top_p       = await _get_runtime_config(tenant_id, f"agent:{agent_id}:top_p",       default="1.0")
    return {
        "agent_id": agent_id,
        "temperature": float(temperature),
        "model": str(model),
        "max_tokens": int(max_tokens),
        "top_p": float(top_p),
    }


@router.post("/agents/{agent_id}/config")
async def set_agent_config(
    agent_id: str,
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Write per-agent inference config to Redis. Orchestrator reads these on next task start."""
    allowed = {"temperature": float, "model": str, "max_tokens": int, "top_p": float}
    saved = {}
    for key, cast in allowed.items():
        if key in body:
            val = cast(body[key])
            await _set_runtime_config(tenant_id, f"agent:{agent_id}:{key}", val)
            saved[key] = val

    await _write_audit_log(tenant_id, "agent_config_update", agent_id, str(saved))
    return {"ok": True, "agent_id": agent_id, "saved": saved}


# ── Cost Attribution endpoint ──────────────────────────────────────────────────

# Approximate cost per 1000 tokens by model family
_COST_PER_1K: dict[str, float] = {
    "claude-3-opus":   0.015,
    "claude-3-sonnet": 0.003,
    "claude-3-haiku":  0.0008,
    "gpt-4":           0.030,
    "gpt-4-turbo":     0.010,
    "gpt-3.5-turbo":   0.001,
    "llama":           0.0,
    "ollama":          0.0,
    "mistral":         0.0,
    "default":         0.001,
}


def _estimate_cost(tokens: int, model: str) -> float:
    if not tokens:
        return 0.0
    model_lower = str(model or "").lower()
    rate = _COST_PER_1K.get("default", 0.001)
    for prefix, cost in _COST_PER_1K.items():
        if prefix in model_lower:
            rate = cost
            break
    return round(tokens / 1000 * rate, 6)


@router.get("/cost-attribution")
async def get_cost_attribution(
    tenant_id: str = Query(default="default"),
    period: str = Query(default="7d"),
    group_by: str = Query(default="agent"),
):
    """Token cost attribution by agent/model/day."""
    days = {"1d": 1, "7d": 7, "30d": 30}.get(period, 7)
    rows: list[dict] = []
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                # Try mv_llm_lineage first (has model column)
                has_lineage = await _table_exists(cur, "mv_llm_lineage")
                if has_lineage:
                    await cur.execute(
                        f"""
                        SELECT agent_name,
                               COALESCE(model, 'unknown') AS model,
                               COALESCE(SUM(COALESCE(input_tokens, 0) + COALESCE(output_tokens, 0)), 0) AS tokens,
                               COUNT(*) AS calls,
                               DATE_TRUNC('day', created_at) AS day
                          FROM mv_llm_lineage
                         WHERE tenant_id = %s
                           AND created_at >= NOW() - INTERVAL '{int(days)} days'
                         GROUP BY agent_name, model, DATE_TRUNC('day', created_at)
                         ORDER BY day DESC, tokens DESC
                        """,
                        (tenant_id,),
                    )
                    raw = await cur.fetchall()
                    for r in raw:
                        rows.append({
                            "agent": r.get("agent_name") or "unknown",
                            "model": r.get("model") or "unknown",
                            "tokens": int(r.get("tokens") or 0),
                            "calls": int(r.get("calls") or 0),
                            "day": str(r.get("day", ""))[:10],
                        })
                else:
                    # fallback: tasks.tokens_used
                    if await _table_exists(cur, "tasks"):
                        await cur.execute(
                            f"""
                            SELECT agent_name,
                                   COALESCE(SUM(tokens_used), 0) AS tokens,
                                   COUNT(*) AS calls,
                                   DATE_TRUNC('day', created_at) AS day
                              FROM tasks
                             WHERE tenant_id = %s
                               AND created_at >= NOW() - INTERVAL '{int(days)} days'
                             GROUP BY agent_name, DATE_TRUNC('day', created_at)
                             ORDER BY day DESC, tokens DESC
                            """,
                            (tenant_id,),
                        )
                        raw = await cur.fetchall()
                        for r in raw:
                            rows.append({
                                "agent": r.get("agent_name") or "unknown",
                                "model": "unknown",
                                "tokens": int(r.get("tokens") or 0),
                                "calls": int(r.get("calls") or 0),
                                "day": str(r.get("day", ""))[:10],
                            })
    except Exception as exc:
        log.debug("cost_attribution_error error=%s", exc)

    # Aggregate by agent for summary
    summary: dict[str, dict] = {}
    for r in rows:
        key = r["agent"] if group_by == "agent" else r.get("model", "unknown")
        if key not in summary:
            summary[key] = {"name": key, "tokens": 0, "calls": 0, "cost_usd": 0.0}
        summary[key]["tokens"] += r["tokens"]
        summary[key]["calls"] += r["calls"]
        summary[key]["cost_usd"] = round(summary[key]["cost_usd"] + _estimate_cost(r["tokens"], r.get("model", "")), 6)

    total_tokens = sum(v["tokens"] for v in summary.values())
    total_cost   = round(sum(v["cost_usd"] for v in summary.values()), 6)

    return {
        "period": period,
        "group_by": group_by,
        "rows": rows,
        "summary": sorted(summary.values(), key=lambda x: x["tokens"], reverse=True),
        "total_tokens": total_tokens,
        "total_cost_usd": total_cost,
    }


# ── PATCH /tasks/{task_id} (edit + re-run) ────────────────────────────────────

@router.patch("/tasks/{task_id}")
async def patch_task(
    task_id: str,
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Edit a task's prompt/priority and optionally re-queue it."""
    allowed_fields = {"prompt", "description", "priority", "agent_name"}
    updates = {k: v for k, v in body.items() if k in allowed_fields}
    rerun = bool(body.get("rerun", False))
    if rerun:
        updates["status"] = "pending"

    if not updates:
        return {"ok": False, "detail": "No valid fields to update"}

    set_clauses = ", ".join(f"{k} = %s" for k in updates)
    values = list(updates.values()) + ["NOW()", tenant_id, task_id]

    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                await cur.execute(
                    f"UPDATE tasks SET {set_clauses}, updated_at = %s "
                    f"WHERE tenant_id = %s AND {task_pk} = %s",
                    values,
                )
                await conn.commit()
    except Exception as exc:
        log.warning("patch_task_error task_id=%s error=%s", task_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))

    action = "task_rerun" if rerun else "task_edit"
    await _write_audit_log(tenant_id, action, task_id, str(updates))
    return {"ok": True, "task_id": task_id, "rerun": rerun, "updated": list(updates.keys())}

# ═══════════════════════════════════════════════════════════════════════════════
# L0 — INFRA CONTROL: circuit breaker, mode, worker scaling, rate limiter
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/infra/status")
async def get_infra_status(tenant_id: str = Query(default="default")):
    """Current state of circuit breaker, operating mode, worker scale, rate limit."""
    cb      = await _get_runtime_config(tenant_id, "circuit_breaker",  default="off")
    mode    = await _get_runtime_config(tenant_id, "operating_mode",   default="balanced")
    workers = await _get_runtime_config(tenant_id, "worker_replicas",  default="2")
    rpm     = await _get_runtime_config(tenant_id, "global_rpm_limit", default="60")
    failover= await _get_runtime_config(tenant_id, "failover_mode",    default="local")
    return {
        "circuit_breaker": str(cb),
        "operating_mode":  str(mode),
        "worker_replicas": int(workers),
        "global_rpm_limit": int(rpm),
        "failover_mode":   str(failover),
    }


@router.post("/infra/circuit-breaker")
async def toggle_circuit_breaker(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Toggle global circuit breaker (halts all LLM + tool calls when ON)."""
    state = "on" if body.get("enabled", False) else "off"
    await _set_runtime_config(tenant_id, "circuit_breaker", state)
    redis_client = get_async_redis()
    if redis_client:
        try:
            await redis_client.publish("config_update", json.dumps({"circuit_breaker": state, "tenant_id": tenant_id}))
        except Exception:
            pass
    await _write_audit_log(tenant_id, "circuit_breaker_toggle", "global", f"state={state}")
    return {"ok": True, "circuit_breaker": state}


@router.post("/infra/mode")
async def set_operating_mode(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Set operating mode: low_cost | balanced | high_performance."""
    allowed = {"low_cost", "balanced", "high_performance"}
    mode = str(body.get("mode", "balanced")).lower()
    if mode not in allowed:
        raise HTTPException(status_code=400, detail=f"mode must be one of {allowed}")
    await _set_runtime_config(tenant_id, "operating_mode", mode)
    # Adjust confidence threshold automatically per mode
    conf_map = {"low_cost": 0.85, "balanced": 0.72, "high_performance": 0.55}
    await _set_runtime_config(tenant_id, "conf_threshold", conf_map[mode])
    redis_client = get_async_redis()
    if redis_client:
        try:
            await redis_client.publish("config_update", json.dumps({"operating_mode": mode, "tenant_id": tenant_id}))
        except Exception:
            pass
    await _write_audit_log(tenant_id, "mode_change", "global", f"mode={mode}")
    return {"ok": True, "operating_mode": mode, "auto_confidence": conf_map[mode]}


@router.post("/infra/scale-workers")
async def scale_workers(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Set number of worker replicas (1-32). Also attempts docker scale if available."""
    replicas = max(1, min(32, int(body.get("replicas", 2))))
    await _set_runtime_config(tenant_id, "worker_replicas", replicas)
    docker_result = "no_docker"
    try:
        import subprocess as _sp
        result = _sp.run(
            ["docker", "compose", "scale", f"worker={replicas}"],
            capture_output=True, text=True, timeout=15
        )
        docker_result = "ok" if result.returncode == 0 else result.stderr.strip()[:120]
    except Exception as exc:
        docker_result = str(exc)[:80]
    await _write_audit_log(tenant_id, "scale_workers", "worker", f"replicas={replicas}")
    return {"ok": True, "worker_replicas": replicas, "docker_result": docker_result}


@router.post("/infra/rate-limiter")
async def set_rate_limiter(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Set global requests-per-minute limit."""
    rpm = max(1, min(10000, int(body.get("rpm", 60))))
    await _set_runtime_config(tenant_id, "global_rpm_limit", rpm)
    await _write_audit_log(tenant_id, "rate_limiter_update", "global", f"rpm={rpm}")
    return {"ok": True, "global_rpm_limit": rpm}


@router.post("/infra/failover")
async def set_failover_mode(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Set failover mode: local | cloud | hybrid."""
    allowed = {"local", "cloud", "hybrid"}
    mode = str(body.get("mode", "local")).lower()
    if mode not in allowed:
        raise HTTPException(status_code=400, detail=f"mode must be one of {allowed}")
    await _set_runtime_config(tenant_id, "failover_mode", mode)
    await _write_audit_log(tenant_id, "failover_change", "global", f"mode={mode}")
    return {"ok": True, "failover_mode": mode}


# ═══════════════════════════════════════════════════════════════════════════════
# L1 — AGENT CONTROL: clone, reassign, version history
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/agents/{agent_id}/clone")
async def clone_agent(
    agent_id: str,
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Clone an agent's config to a new name."""
    new_name = str(body.get("new_name", f"{agent_id}_clone")).strip()
    if not new_name or len(new_name) < 2:
        raise HTTPException(status_code=400, detail="new_name must be at least 2 chars")
    # Copy all config keys from source to new agent
    for key in ("temperature", "model", "max_tokens", "top_p"):
        val = await _get_runtime_config(tenant_id, f"agent:{agent_id}:{key}")
        if val is not None:
            await _set_runtime_config(tenant_id, f"agent:{new_name}:{key}", val)
    await _write_audit_log(tenant_id, "agent_clone", agent_id, f"new_name={new_name}")
    return {"ok": True, "source": agent_id, "clone": new_name}


@router.post("/agents/{agent_id}/reassign")
async def reassign_agent_task(
    agent_id: str,
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Reassign all running/pending tasks from one agent to another."""
    target_agent = str(body.get("target_agent", "")).strip()
    if not target_agent:
        raise HTTPException(status_code=400, detail="target_agent required")
    moved = 0
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                await cur.execute(
                    f"UPDATE tasks SET agent_name = %s, updated_at = NOW() "
                    f"WHERE tenant_id = %s AND agent_name = %s AND status IN ('pending','running','paused') "
                    f"RETURNING {task_pk}",
                    (target_agent, tenant_id, agent_id),
                )
                moved = len(await cur.fetchall())
                await conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    await _write_audit_log(tenant_id, "agent_reassign", agent_id, f"target={target_agent} moved={moved}")
    return {"ok": True, "from": agent_id, "to": target_agent, "tasks_moved": moved}


# ═══════════════════════════════════════════════════════════════════════════════
# L2 — COGNITIVE ORCHESTRATION: reputation controls, auto-learning
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/intelligence/reputation/{agent_id}/adjust")
async def adjust_agent_reputation(
    agent_id: str,
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Manually adjust an agent's reputation score offset stored in Redis."""
    delta = float(body.get("delta", 0))  # +/- percentage points
    if abs(delta) > 100:
        raise HTTPException(status_code=400, detail="delta must be between -100 and +100")
    key = f"agent:{agent_id}:rep_offset"
    current = float(await _get_runtime_config(tenant_id, key, default="0") or 0)
    new_val  = max(-100.0, min(100.0, current + delta))
    await _set_runtime_config(tenant_id, key, new_val)
    action = "rep_boost" if delta > 0 else "rep_penalty"
    await _write_audit_log(tenant_id, action, agent_id, f"delta={delta:+.1f} new_offset={new_val:.1f}")
    return {"ok": True, "agent": agent_id, "delta": delta, "new_offset": new_val}


@router.post("/intelligence/reputation/auto-learning")
async def set_auto_learning(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Toggle auto-learning (reputation updates from task outcomes)."""
    enabled = bool(body.get("enabled", True))
    await _set_runtime_config(tenant_id, "auto_learning", "on" if enabled else "off")
    redis_client = get_async_redis()
    if redis_client:
        try:
            await redis_client.publish("config_update", json.dumps({"auto_learning": enabled, "tenant_id": tenant_id}))
        except Exception:
            pass
    await _write_audit_log(tenant_id, "auto_learning_toggle", "global", f"enabled={enabled}")
    return {"ok": True, "auto_learning": enabled}


# ═══════════════════════════════════════════════════════════════════════════════
# L3 — TASK CONTROL: duplicate, full details, timeout config
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/tasks/{task_id}/duplicate")
async def duplicate_task(
    task_id: str,
    tenant_id: str = Query(default="default"),
):
    """Clone a task into a new pending task."""
    import uuid as _uuid
    new_id = f"dup-{_uuid.uuid4().hex[:8]}"
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                cols = await get_table_columns_cached(cur, "tasks")
                copy_cols = [c for c in cols if c not in (task_pk, "id", "created_at", "updated_at", "status")]
                select_cols = ", ".join(copy_cols)
                await cur.execute(
                    f"SELECT {select_cols} FROM tasks WHERE tenant_id = %s AND {task_pk} = %s",
                    (tenant_id, task_id),
                )
                src = await cur.fetchone()
                if not src:
                    raise HTTPException(status_code=404, detail="Task not found")
                vals = [new_id, tenant_id] + [src.get(c) for c in copy_cols if c not in ("task_id", "tenant_id")]
                placeholders = ", ".join(["%s"] * (len(copy_cols) + 1))
                insert_cols = ", ".join(["task_id"] + [c for c in copy_cols if c not in ("task_id",)])
                await cur.execute(
                    f"INSERT INTO tasks ({insert_cols}, status, created_at, updated_at) "
                    f"VALUES ({placeholders}, 'pending', NOW(), NOW()) ON CONFLICT DO NOTHING",
                    [new_id] + [src.get(c) for c in copy_cols if c not in ("task_id",)],
                )
                await conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        log.warning("duplicate_task_error task_id=%s error=%s", task_id, exc)
        raise HTTPException(status_code=500, detail=str(exc))
    await _write_audit_log(tenant_id, "task_duplicate", task_id, f"new_id={new_id}")
    return {"ok": True, "source_task_id": task_id, "new_task_id": new_id}


@router.post("/infra/zombie-timeout")
async def set_zombie_timeout(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Configure zombie detection timeout in minutes."""
    minutes = max(1, min(1440, int(body.get("minutes", 10))))
    await _set_runtime_config(tenant_id, "zombie_timeout_minutes", minutes)
    await _write_audit_log(tenant_id, "zombie_timeout_update", "global", f"minutes={minutes}")
    return {"ok": True, "zombie_timeout_minutes": minutes}


# ═══════════════════════════════════════════════════════════════════════════════
# L4 — MEMORY CONTROL: vector search playground, pruning
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/memory/vector-search")
async def memory_vector_search(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Search Qdrant vector store directly from the playground."""
    query      = str(body.get("query", "")).strip()
    project_id = str(body.get("project_id", "project0"))
    top_k      = min(20, max(1, int(body.get("top_k", 5))))
    threshold  = float(body.get("threshold", 0.0))
    if not query:
        raise HTTPException(status_code=400, detail="query required")
    try:
        from services.context_retriever import ContextRetriever
        retriever = ContextRetriever()
        result = retriever.retrieve(query=query, project_id=project_id, tenant_id=tenant_id, top_k=top_k)
        # Filter by threshold
        if threshold > 0 and isinstance(result, list):
            result = [r for r in result if (r.get("score") or 0) >= threshold]
        return {"ok": True, "query": query, "results": result, "count": len(result) if isinstance(result, list) else 0}
    except Exception as exc:
        log.warning("vector_search_error error=%s", exc)
        return {"ok": False, "error": str(exc), "results": []}


@router.delete("/memory/lessons/{lesson_id}")
async def delete_lesson(
    lesson_id: str,
    tenant_id: str = Query(default="default"),
):
    """Delete a lesson from the lessons_learned table by ID."""
    deleted = False
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if not await _table_exists(cur, "lessons_learned"):
                    raise HTTPException(status_code=503, detail="lessons_learned table unavailable")
                await cur.execute(
                    "DELETE FROM lessons_learned WHERE id = %s AND (tenant_id = %s OR tenant_id IS NULL)",
                    (lesson_id, tenant_id),
                )
                deleted = cur.rowcount > 0
                await conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    await _write_audit_log(tenant_id, "lesson_delete", lesson_id, "pruned")
    return {"ok": deleted, "lesson_id": lesson_id}


@router.post("/memory/vector-prune")
async def prune_vector(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Delete a specific vector from Qdrant by ID."""
    vector_id = str(body.get("id", "")).strip()
    collection = str(body.get("collection", "sinc_memory"))
    if not vector_id:
        raise HTTPException(status_code=400, detail="id required")
    try:
        from qdrant_client import QdrantClient
        import os
        client = QdrantClient(host=os.environ.get("QDRANT_HOST", "localhost"), port=int(os.environ.get("QDRANT_PORT", 6333)))
        client.delete(collection_name=collection, points_selector=[vector_id])
        await _write_audit_log(tenant_id, "vector_prune", f"{collection}/{vector_id}", "deleted")
        return {"ok": True, "id": vector_id, "collection": collection}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
# L5 — DEEP TRACE: full task trace, tool stats
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/tasks/{task_id}/trace")
async def get_task_trace(
    task_id: str,
    tenant_id: str = Query(default="default"),
):
    """Full execution trace for a task: prompt, response, tools, timing."""
    result: dict = {"task_id": task_id, "found": False}
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                task_pk = await get_task_pk_column(cur)
                cols    = await get_table_columns_cached(cur, "tasks")
                await cur.execute(
                    f"SELECT * FROM tasks WHERE tenant_id = %s AND {task_pk} = %s",
                    (tenant_id, task_id),
                )
                row = await cur.fetchone()
                if row:
                    result.update(dict(row))
                    result["found"] = True

                # Tool calls from mv_llm_lineage if available
                if await _table_exists(cur, "mv_llm_lineage"):
                    await cur.execute(
                        "SELECT * FROM mv_llm_lineage WHERE task_id = %s ORDER BY created_at",
                        (task_id,),
                    )
                    result["llm_calls"] = await cur.fetchall()
    except Exception as exc:
        log.debug("task_trace_error task_id=%s error=%s", task_id, exc)
    return result


@router.get("/diagnostics/tool-stats")
async def get_tool_stats(
    tenant_id: str = Query(default="default"),
    period: str = Query(default="24h"),
):
    """Tool usage statistics: calls per tool, avg latency, failure rate."""
    hours = {"1h": 1, "24h": 24, "7d": 168}.get(period, 24)
    stats: list = []
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if await _table_exists(cur, "mv_llm_lineage"):
                    await cur.execute(
                        f"""
                        SELECT model AS tool_name,
                               COUNT(*) AS calls,
                               AVG(latency_ms) AS avg_latency_ms,
                               COUNT(*) FILTER (WHERE status IN ('error','failed')) AS failures
                          FROM mv_llm_lineage
                         WHERE tenant_id = %s AND created_at >= NOW() - INTERVAL '{int(hours)} hours'
                         GROUP BY model
                         ORDER BY calls DESC
                        """,
                        (tenant_id,),
                    )
                    stats = await cur.fetchall()
    except Exception as exc:
        log.debug("tool_stats_error error=%s", exc)
    return {"period": period, "stats": stats}


# ═══════════════════════════════════════════════════════════════════════════════
# L6 — SECURITY: API keys management
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/security/api-keys")
async def list_api_keys(tenant_id: str = Query(default="default")):
    """List API keys for the tenant (masked)."""
    keys: list = []
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if await _table_exists(cur, "api_keys"):
                    await cur.execute(
                        """
                        SELECT id, key, label, created_at, last_used_at, revoked_at
                          FROM api_keys
                         WHERE tenant_id = %s
                         ORDER BY created_at DESC
                        """,
                        (tenant_id,),
                    )
                    for row in await cur.fetchall():
                        k = dict(row)
                        # Mask key: show first 8 + last 4 chars
                        raw = str(k.get("key", ""))
                        k["key_masked"] = raw[:8] + "****" + raw[-4:] if len(raw) > 12 else raw[:4] + "****"
                        k.pop("key", None)
                        keys.append(k)
    except Exception as exc:
        log.debug("list_api_keys_error error=%s", exc)
    return {"keys": keys, "count": len(keys)}


@router.post("/security/api-keys")
async def create_api_key(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Create a new API key for the tenant."""
    import secrets as _sec
    import hashlib as _hl
    label = str(body.get("label", "noc-generated")).strip()[:64]
    raw_key = "sk-" + _sec.token_hex(24)
    key_hash = _hl.sha256(raw_key.encode()).hexdigest()
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                if not await _table_exists(cur, "api_keys"):
                    raise HTTPException(status_code=503, detail="api_keys table unavailable")
                await cur.execute(
                    "INSERT INTO api_keys (tenant_id, key, label, created_at) VALUES (%s, %s, %s, NOW())",
                    (tenant_id, raw_key, label),
                )
                await conn.commit()
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    await _write_audit_log(tenant_id, "api_key_create", label, "new key issued")
    return {"ok": True, "api_key": raw_key, "label": label}  # shown once


@router.delete("/security/api-keys/{key_id}")
async def revoke_api_key(
    key_id: str,
    tenant_id: str = Query(default="default"),
):
    """Revoke an API key by ID."""
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE api_keys SET revoked_at = NOW() WHERE id = %s AND tenant_id = %s",
                    (key_id, tenant_id),
                )
                await conn.commit()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    await _write_audit_log(tenant_id, "api_key_revoke", key_id, "revoked")
    return {"ok": True, "key_id": key_id, "revoked": True}


@router.get("/security/anomalies")
async def get_security_anomalies(
    tenant_id: str = Query(default="default"),
    limit: int = Query(default=20, le=100),
):
    """Return recent agent anomaly events (zombie spikes, prompt injection patterns, tool abuse)."""
    anomalies: list = []
    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                # Zombie spike: agents stuck > 30 min
                if await _table_exists(cur, "tasks"):
                    await cur.execute(
                        """
                        SELECT 'zombie_spike' AS type, agent_name AS target,
                               COUNT(*) AS count,
                               MAX(updated_at) AS ts,
                               'Agent stuck > 30 minutes' AS detail
                          FROM tasks
                         WHERE tenant_id = %s AND status = 'running'
                           AND updated_at < NOW() - INTERVAL '30 minutes'
                         GROUP BY agent_name
                         HAVING COUNT(*) > 0
                        """,
                        (tenant_id,),
                    )
                    anomalies.extend(await cur.fetchall())

                    # Token abuse: agent using > 2x average tokens in last hour
                    await cur.execute(
                        """
                        WITH avg_tok AS (
                          SELECT AVG(tokens_used) AS avg_t FROM tasks WHERE tenant_id = %s
                        )
                        SELECT 'token_abuse' AS type, agent_name AS target,
                               SUM(tokens_used) AS count,
                               MAX(created_at) AS ts,
                               'Token usage > 2x average' AS detail
                          FROM tasks, avg_tok
                         WHERE tenant_id = %s
                           AND created_at >= NOW() - INTERVAL '1 hour'
                           AND tokens_used > avg_t * 2
                         GROUP BY agent_name
                         HAVING SUM(tokens_used) > 0
                        """,
                        (tenant_id, tenant_id),
                    )
                    anomalies.extend(await cur.fetchall())

                    # Failure spike: agent with > 50% failure rate in last hour
                    await cur.execute(
                        """
                        SELECT 'failure_spike' AS type, agent_name AS target,
                               COUNT(*) AS count,
                               MAX(updated_at) AS ts,
                               'Failure rate > 50% in last hour' AS detail
                          FROM tasks
                         WHERE tenant_id = %s
                           AND created_at >= NOW() - INTERVAL '1 hour'
                         GROUP BY agent_name
                         HAVING COUNT(*) FILTER (WHERE status = 'failed')::float / NULLIF(COUNT(*), 0) > 0.5
                        """,
                        (tenant_id,),
                    )
                    anomalies.extend(await cur.fetchall())
    except Exception as exc:
        log.debug("anomalies_error error=%s", exc)

    # Sort by ts desc
    def _ts(a):
        t = a.get("ts")
        return str(t) if t else ""
    anomalies.sort(key=_ts, reverse=True)
    return {"anomalies": anomalies[:limit], "count": len(anomalies)}


# ═══════════════════════════════════════════════════════════════════════════════
# L9 — EVENT ALERTS: thresholds, webhook config
# ═══════════════════════════════════════════════════════════════════════════════

@router.get("/alerts/config")
async def get_alerts_config(tenant_id: str = Query(default="default")):
    """Get alert threshold configuration."""
    cpu_thresh    = await _get_runtime_config(tenant_id, "alert:cpu_pct",        default="85")
    ram_thresh    = await _get_runtime_config(tenant_id, "alert:ram_pct",        default="88")
    zombie_thresh = await _get_runtime_config(tenant_id, "alert:zombie_count",   default="3")
    fail_thresh   = await _get_runtime_config(tenant_id, "alert:fail_rate_pct",  default="30")
    webhook_url   = await _get_runtime_config(tenant_id, "alert:webhook_url",    default="")
    webhook_type  = await _get_runtime_config(tenant_id, "alert:webhook_type",   default="slack")
    alerts_on     = await _get_runtime_config(tenant_id, "alert:enabled",        default="true")
    return {
        "cpu_pct":       int(cpu_thresh),
        "ram_pct":       int(ram_thresh),
        "zombie_count":  int(zombie_thresh),
        "fail_rate_pct": int(fail_thresh),
        "webhook_url":   str(webhook_url),
        "webhook_type":  str(webhook_type),
        "enabled":       str(alerts_on).lower() == "true",
    }


@router.post("/alerts/config")
async def save_alerts_config(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Save alert threshold + webhook configuration."""
    mapping = {
        "cpu_pct":       "alert:cpu_pct",
        "ram_pct":       "alert:ram_pct",
        "zombie_count":  "alert:zombie_count",
        "fail_rate_pct": "alert:fail_rate_pct",
        "webhook_url":   "alert:webhook_url",
        "webhook_type":  "alert:webhook_type",
        "enabled":       "alert:enabled",
    }
    saved = {}
    for field, config_key in mapping.items():
        if field in body:
            val = body[field]
            await _set_runtime_config(tenant_id, config_key, val)
            saved[field] = val
    await _write_audit_log(tenant_id, "alerts_config_update", "global", str(saved))
    return {"ok": True, "saved": saved}


@router.post("/alerts/test-webhook")
async def test_webhook(
    tenant_id: str = Query(default="default"),
):
    """Send a test alert to the configured webhook."""
    import aiohttp
    url  = str(await _get_runtime_config(tenant_id, "alert:webhook_url", default="") or "")
    wtype = str(await _get_runtime_config(tenant_id, "alert:webhook_type", default="slack") or "slack")
    if not url:
        return {"ok": False, "error": "No webhook URL configured"}
    payload = {"text": "[SINC NOC] Test alert — webhook configured correctly ✓"} if wtype == "slack" \
        else {"content": "[SINC NOC] Test alert — webhook configured correctly ✓"}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                return {"ok": resp.status < 300, "status": resp.status}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ═══════════════════════════════════════════════════════════════════════════════
# L10 — META CONTROL: dry-run simulation, cost prediction
# ═══════════════════════════════════════════════════════════════════════════════

@router.post("/simulate/dry-run")
async def simulate_dry_run(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """
    Predict cost + execution path for a task WITHOUT running it.
    Uses historical data to estimate: agent selection, token usage, duration.
    """
    prompt    = str(body.get("prompt", "")).strip()
    agent     = str(body.get("agent_name", "")).strip()
    priority  = int(body.get("priority", 5))

    if not prompt:
        raise HTTPException(status_code=400, detail="prompt required")

    prediction = {
        "prompt_chars":      len(prompt),
        "estimated_tokens":  max(100, len(prompt.split()) * 6),  # rough heuristic
        "selected_agent":    agent or "auto",
        "estimated_duration_s": None,
        "estimated_cost_usd": None,
        "queue_position":    None,
        "confidence":        0.0,
        "similar_tasks":     [],
    }

    try:
        async with async_db(tenant_id=tenant_id) as conn:
            async with conn.cursor() as cur:
                # Similar past tasks
                cols = await get_table_columns_cached(cur, "tasks")
                if "tokens_used" in cols and "prompt" in cols:
                    await cur.execute(
                        """
                        SELECT agent_name,
                               AVG(tokens_used)                                         AS avg_tokens,
                               AVG(EXTRACT(EPOCH FROM (updated_at - created_at)))       AS avg_duration_s,
                               COUNT(*) FILTER (WHERE status IN ('done','completed','success'))::float / NULLIF(COUNT(*),0) AS success_rate
                          FROM tasks
                         WHERE tenant_id = %s
                           AND status IN ('done','completed','success','failed')
                           AND agent_name = COALESCE(NULLIF(%s,''), agent_name)
                         GROUP BY agent_name
                         ORDER BY avg_tokens
                         LIMIT 5
                        """,
                        (tenant_id, agent or None),
                    )
                    similar = await cur.fetchall()
                    if similar:
                        best = similar[0]
                        est_tokens   = int(best.get("avg_tokens") or prediction["estimated_tokens"])
                        est_duration = float(best.get("avg_duration_s") or 30)
                        success_rate = float(best.get("success_rate") or 0.7)
                        prediction.update({
                            "selected_agent":      best.get("agent_name") or agent or "auto",
                            "estimated_tokens":    est_tokens,
                            "estimated_duration_s": round(est_duration, 1),
                            "estimated_cost_usd":  _estimate_cost(est_tokens, ""),
                            "confidence":          round(success_rate * 100, 1),
                            "similar_tasks":       [dict(r) for r in similar[:3]],
                        })

                # Queue position
                if await _table_exists(cur, "tasks"):
                    await cur.execute(
                        "SELECT COUNT(*) AS n FROM tasks WHERE tenant_id = %s AND status IN ('pending','running') AND priority >= %s",
                        (tenant_id, priority),
                    )
                    q_row = await cur.fetchone()
                    prediction["queue_position"] = int(q_row["n"] or 0) + 1 if q_row else 1

    except Exception as exc:
        log.debug("dry_run_error error=%s", exc)

    return {"ok": True, "simulation": prediction}


# ═══════════════════════════════════════════════════════════════════
# METRICS TRENDS — time-series for home sparklines
# ═══════════════════════════════════════════════════════════════════

@router.get("/metrics/trends")
async def get_metrics_trends(
    tenant_id: str = Query(default="default"),
    window_minutes: int = Query(default=60),
    points: int = Query(default=20),
):
    """
    Returns time-series arrays for home dashboard sparklines.
    Uses a SINGLE SQL query with bucket arithmetic — O(1) round-trips.
    """
    points      = min(max(points, 5), 30)
    bucket_sec  = max(60, (window_minutes * 60) // points)
    now_ts      = _time.time()
    start_ts    = now_ts - window_minutes * 60

    # Pre-fill buckets so gaps stay null (not 0)
    bucket_map: dict[int, dict] = {}
    for i in range(points):
        ts = int(start_ts + i * bucket_sec)
        bucket_map[ts] = {"done": 0, "total": 0, "queue": 0, "agents": set(), "tokens": 0, "lat_sum": 0.0, "lat_n": 0}

    try:
        async with async_db() as cur:
            if not await _table_exists(cur, "tasks"):
                raise ValueError("no tasks table")

            cols = await get_table_columns_cached(cur, "tasks")
            has_tokens   = "tokens_used" in cols
            has_duration = "duration"    in cols

            tok_expr = "COALESCE(tokens_used, 0)"  if has_tokens   else "0"
            dur_expr = "COALESCE(duration,   0.0)" if has_duration else "0.0"

            await cur.execute(
                f"""
                SELECT
                    (FLOOR(EXTRACT(EPOCH FROM updated_at) / %(bsec)s) * %(bsec)s)::bigint  AS bucket,
                    COUNT(*)                                              AS total,
                    COUNT(*) FILTER (WHERE status = 'done')              AS done,
                    COUNT(*) FILTER (WHERE status IN ('pending','running')) AS queue_n,
                    COUNT(DISTINCT agent_name) FILTER (WHERE status = 'running') AS agents,
                    COALESCE(SUM({tok_expr}), 0)                         AS tokens,
                    COALESCE(AVG({dur_expr}) FILTER (WHERE status = 'done'), 0) AS avg_dur
                FROM tasks
                WHERE tenant_id = %(tid)s
                  AND updated_at >= to_timestamp(%(start)s)
                GROUP BY bucket
                ORDER BY bucket
                """,
                {"tid": tenant_id, "bsec": bucket_sec, "start": start_ts},
            )
            rows = await cur.fetchall()

            for row in rows:
                bk = int(row["bucket"] or 0)
                # snap to nearest pre-filled bucket
                nearest = min(bucket_map, key=lambda k: abs(k - bk))
                b = bucket_map[nearest]
                b["done"]    += int(row["done"]    or 0)
                b["total"]   += int(row["total"]   or 0)
                b["queue"]   += int(row["queue_n"] or 0)
                b["agents"]  = max(b["agents"] if isinstance(b["agents"], int) else 0,
                                   int(row["agents"] or 0))
                b["tokens"]  += int(row["tokens"]  or 0)
                avg_d = float(row["avg_dur"] or 0)
                if avg_d > 0:
                    b["lat_sum"] += avg_d * 1000
                    b["lat_n"]   += 1

    except Exception as exc:
        log.debug("metrics_trends error=%s", exc)

    # Serialise
    series: dict[str, list] = {
        "timestamps": [], "success_rate": [], "queue_depth": [],
        "active_agents": [], "tokens_per_min": [], "avg_latency_ms": [],
    }
    for ts in sorted(bucket_map):
        b = bucket_map[ts]
        total = b["total"]
        series["timestamps"].append(ts)
        series["success_rate"].append(round(b["done"] / total, 3) if total else None)
        series["queue_depth"].append(b["queue"] or None)
        series["active_agents"].append(b["agents"] if isinstance(b["agents"], int) and b["agents"] > 0 else None)
        series["tokens_per_min"].append(round(b["tokens"] / max(1, bucket_sec / 60), 1) if b["tokens"] else None)
        series["avg_latency_ms"].append(round(b["lat_sum"] / b["lat_n"], 0) if b["lat_n"] else None)

    return {"ok": True, "window_minutes": window_minutes, "points": points,
            "bucket_seconds": bucket_sec, "series": series}


# ═══════════════════════════════════════════════════════════════════
# WORKERS — list active worker processes
# ═══════════════════════════════════════════════════════════════════

@router.get("/workers")
async def list_workers(
    tenant_id: str = Query(default="default"),
):
    """List worker agents — from Redis heartbeats + DB running tasks."""
    workers = []
    try:
        redis = await get_async_redis()
        if redis:
            # scan for worker heartbeat keys
            pattern = "sinc:worker:*"
            cursor = 0
            keys = []
            while True:
                cursor, batch = await redis.scan(cursor, match=pattern, count=100)
                keys.extend(batch)
                if cursor == 0:
                    break
            for key in keys[:50]:
                try:
                    raw = await redis.get(key)
                    if raw:
                        w = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                        workers.append(w)
                except Exception:
                    pass
    except Exception:
        pass

    # Supplement from DB: running tasks with agent_name
    try:
        async with async_db() as cur:
            if await _table_exists(cur, "tasks"):
                await cur.execute(
                    """SELECT agent_name,
                        COUNT(*) AS running_tasks,
                        MIN(created_at) AS oldest_task,
                        MAX(updated_at) AS last_update,
                        AVG(EXTRACT(EPOCH FROM (NOW() - created_at))) AS avg_run_secs
                    FROM tasks
                    WHERE tenant_id = %s AND status = 'running'
                    GROUP BY agent_name
                    ORDER BY running_tasks DESC
                    LIMIT 30
                    """,
                    (tenant_id,),
                )
                rows = await cur.fetchall()
                db_agents = {w.get("name") or w.get("agent_name"): w for w in workers}
                for row in rows:
                    name = row["agent_name"] or "unknown"
                    if name not in db_agents:
                        workers.append({
                            "name": name,
                            "status": "running",
                            "running_tasks": int(row["running_tasks"] or 0),
                            "avg_run_secs": round(float(row["avg_run_secs"] or 0), 1),
                            "last_update": str(row["last_update"]) if row.get("last_update") else None,
                            "source": "db",
                        })
                    else:
                        db_agents[name]["running_tasks"] = int(row["running_tasks"] or 0)
    except Exception as exc:
        log.debug("list_workers db error=%s", exc)

    # If nothing found, return a synthetic default worker entry
    if not workers:
        workers = [{"name": "sinc-worker-default", "status": "idle", "running_tasks": 0, "source": "fallback"}]

    return {"ok": True, "count": len(workers), "workers": workers}


# ═══════════════════════════════════════════════════════════════════
# GOALS — read/write active orchestrator goals
# ═══════════════════════════════════════════════════════════════════

@router.get("/goals")
async def get_goals(
    tenant_id: str = Query(default="default"),
    status: str = Query(default=""),
):
    """List orchestrator goals from tasks with type='goal' or a dedicated goals table."""
    goals = []
    try:
        async with async_db() as cur:
            # Try dedicated goals table first
            if await _table_exists(cur, "goals"):
                q = "SELECT * FROM goals WHERE tenant_id = %s"
                params = [tenant_id]
                if status:
                    q += " AND status = %s"
                    params.append(status)
                q += " ORDER BY created_at DESC LIMIT 50"
                await cur.execute(q, params)
                rows = await cur.fetchall()
                goals = [dict(r) for r in rows]
            elif await _table_exists(cur, "tasks"):
                # Synthesize goals from high-priority pending/running tasks
                await cur.execute(
                    """SELECT id, description, status, priority, agent_name,
                        created_at, updated_at,
                        EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_secs
                    FROM tasks
                    WHERE tenant_id = %s
                      AND priority >= 8
                      AND status IN ('pending', 'running', 'done')
                    ORDER BY priority DESC, created_at DESC
                    LIMIT 20
                    """,
                    (tenant_id,),
                )
                rows = await cur.fetchall()
                for r in rows:
                    goals.append({
                        "id": str(r["id"]),
                        "title": (str(r.get("description") or "")[:80]) or "Task #" + str(r["id"]),
                        "status": r["status"],
                        "priority": r["priority"],
                        "agent": r.get("agent_name") or "unassigned",
                        "age_secs": round(float(r["age_secs"] or 0), 0),
                        "source": "tasks",
                    })
    except Exception as exc:
        log.debug("get_goals error=%s", exc)

    return {"ok": True, "count": len(goals), "goals": goals}


@router.post("/goals")
async def create_goal(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Create a new goal (injected as high-priority task)."""
    title = body.get("title", "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="title required")

    goal_id = None
    try:
        async with async_db() as cur:
            if await _table_exists(cur, "goals"):
                await cur.execute(
                    "INSERT INTO goals (tenant_id, title, status, priority) VALUES (%s, %s, 'pending', %s) RETURNING id",
                    (tenant_id, title, body.get("priority", 8)),
                )
                row = await cur.fetchone()
                goal_id = str(row["id"]) if row else None
            elif await _table_exists(cur, "tasks"):
                await cur.execute(
                    "INSERT INTO tasks (tenant_id, description, status, priority) VALUES (%s, %s, 'pending', %s) RETURNING id",
                    (tenant_id, title, body.get("priority", 9)),
                )
                row = await cur.fetchone()
                goal_id = str(row["id"]) if row else None
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    await _write_audit_log(tenant_id, "goal_create", title, f"priority={body.get('priority',8)}")
    return {"ok": True, "id": goal_id, "title": title}


@router.patch("/goals/{goal_id}")
async def update_goal(
    goal_id: str,
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Update goal status or priority."""
    try:
        async with async_db() as cur:
            table = "goals" if await _table_exists(cur, "goals") else "tasks"
            pk = "id"
            updates = []
            params = []
            if "status" in body:
                updates.append("status = %s"); params.append(body["status"])
            if "priority" in body:
                updates.append("priority = %s"); params.append(body["priority"])
            if not updates:
                return {"ok": True, "message": "nothing to update"}
            params.extend([tenant_id, goal_id])
            await cur.execute(
                f"UPDATE {table} SET {', '.join(updates)} WHERE tenant_id = %s AND {pk} = %s",
                params,
            )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    await _write_audit_log(tenant_id, "goal_update", goal_id, str(body))
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════
# TASK STEPS — full execution trace for Deep Trace modal
# ═══════════════════════════════════════════════════════════════════

@router.get("/tasks/{task_id}/steps")
async def get_task_steps(
    task_id: str,
    tenant_id: str = Query(default="default"),
):
    """
    Returns detailed step-by-step execution log for a task.
    Combines: task_steps table → llm_calls table → audit_log entries.
    """
    steps = []
    task_info = {}

    try:
        async with async_db() as cur:
            # Basic task info
            if await _table_exists(cur, "tasks"):
                pk = await get_task_pk_column(cur)
                await cur.execute(
                    f"SELECT * FROM tasks WHERE {pk} = %s AND tenant_id = %s LIMIT 1",
                    (task_id, tenant_id),
                )
                row = await cur.fetchone()
                if row:
                    task_info = dict(row)
                    for k, v in task_info.items():
                        if hasattr(v, "isoformat"):
                            task_info[k] = v.isoformat()

            # Try task_steps table
            if await _table_exists(cur, "task_steps"):
                await cur.execute(
                    "SELECT * FROM task_steps WHERE task_id = %s ORDER BY step_number, created_at LIMIT 200",
                    (task_id,),
                )
                rows = await cur.fetchall()
                for r in rows:
                    s = dict(r)
                    for k, v in s.items():
                        if hasattr(v, "isoformat"):
                            s[k] = v.isoformat()
                    s["_source"] = "task_steps"
                    steps.append(s)

            # Try llm_calls / llm_lineage
            for tbl in ("mv_llm_lineage", "llm_calls"):
                if await _table_exists(cur, tbl):
                    await cur.execute(
                        f"SELECT * FROM {tbl} WHERE task_id = %s ORDER BY created_at LIMIT 100",
                        (task_id,),
                    )
                    rows = await cur.fetchall()
                    for r in rows:
                        s = dict(r)
                        for k, v in s.items():
                            if hasattr(v, "isoformat"):
                                s[k] = v.isoformat()
                        s["_source"] = tbl
                        steps.append(s)
                    if rows:
                        break

            # Try tool_calls
            if await _table_exists(cur, "tool_calls"):
                await cur.execute(
                    "SELECT * FROM tool_calls WHERE task_id = %s ORDER BY created_at LIMIT 100",
                    (task_id,),
                )
                rows = await cur.fetchall()
                for r in rows:
                    s = dict(r)
                    for k, v in s.items():
                        if hasattr(v, "isoformat"):
                            s[k] = v.isoformat()
                    s["_source"] = "tool_calls"
                    s["step_type"] = "tool_call"
                    steps.append(s)

    except Exception as exc:
        log.debug("get_task_steps error=%s", exc)

    # sort by created_at if present
    steps.sort(key=lambda s: s.get("created_at") or s.get("step_number") or 0)

    return {"ok": True, "task": task_info, "step_count": len(steps), "steps": steps}


# ═══════════════════════════════════════════════════════════════════
# QUEUE STATS — real-time queue depth breakdown
# ═══════════════════════════════════════════════════════════════════

@router.get("/queue/stats")
async def get_queue_stats(
    tenant_id: str = Query(default="default"),
):
    """Queue depth breakdown by status, priority, and agent."""
    stats = {
        "by_status": {},
        "by_priority": {},
        "by_agent": {},
        "total": 0,
        "stale_running": 0,
    }
    try:
        async with async_db() as cur:
            if not await _table_exists(cur, "tasks"):
                return {"ok": True, **stats}

            # By status
            await cur.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE tenant_id = %s AND status IN ('pending','running','review') GROUP BY status",
                (tenant_id,),
            )
            for r in await cur.fetchall():
                stats["by_status"][r["status"]] = int(r["n"])
                stats["total"] += int(r["n"])

            # By priority (pending only)
            await cur.execute(
                "SELECT priority, COUNT(*) AS n FROM tasks WHERE tenant_id = %s AND status = 'pending' GROUP BY priority ORDER BY priority DESC LIMIT 10",
                (tenant_id,),
            )
            for r in await cur.fetchall():
                stats["by_priority"][str(r["priority"])] = int(r["n"])

            # By agent (running)
            await cur.execute(
                "SELECT agent_name, COUNT(*) AS n FROM tasks WHERE tenant_id = %s AND status = 'running' GROUP BY agent_name ORDER BY n DESC LIMIT 15",
                (tenant_id,),
            )
            for r in await cur.fetchall():
                stats["by_agent"][r["agent_name"] or "unknown"] = int(r["n"])

            # Stale running (zombie candidates)
            stale_m = int(await _get_runtime_config(tenant_id, "zombie_timeout_minutes") or 10)
            await cur.execute(
                "SELECT COUNT(*) AS n FROM tasks WHERE tenant_id = %s AND status = 'running' AND updated_at < NOW() - INTERVAL '%s minutes'",
                (tenant_id, stale_m),
            )
            sr = await cur.fetchone()
            stats["stale_running"] = int(sr["n"] or 0) if sr else 0

    except Exception as exc:
        log.debug("queue_stats error=%s", exc)

    return {"ok": True, **stats}


# ─── N5 NEURAL STEERING ────────────────────────────────────────────────────────

@router.post("/neural/steer")
async def neural_steer(
    body: dict,
    tenant_id: str = Query(default="default"),
):
    """Inject a correction vector into a running agent's context.

    The vector is stored in Redis under key ``sinc:neural_steer:{task_id}`` with
    a 10-minute TTL.  The agent reads this key on its next reasoning iteration and
    prepends it as a high-priority system message.
    """
    task_id  = body.get("task_id")
    steer_type = body.get("steer_type", "context_inject")
    intensity  = min(max(int(body.get("intensity", 5)), 1), 10)
    payload    = (body.get("payload") or "").strip()
    agent      = body.get("agent", "")

    if not task_id or not payload:
        raise HTTPException(400, detail="task_id and payload required")

    entry = {
        "task_id":   task_id,
        "agent":     agent,
        "type":      steer_type,
        "intensity": intensity,
        "payload":   payload,
        "ts":        _time.time(),
        "tenant_id": tenant_id,
    }

    # Store in Redis for the agent to pick up
    try:
        redis = await get_async_redis()
        key = f"sinc:neural_steer:{task_id}"
        await redis.set(key, json.dumps(entry), ex=600)  # 10 min TTL
        # Also push to the agent's pending-steer list (LPUSH)
        list_key = f"sinc:agent_steers:{tenant_id}"
        await redis.lpush(list_key, json.dumps(entry))
        await redis.expire(list_key, 3600)
    except Exception as exc:
        log.warning("neural_steer redis error=%s", exc)

    # Audit log
    await _write_audit_log(tenant_id, f"neural_steer:{steer_type}", agent or f"task#{task_id}", payload[:120])

    return {"ok": True, "task_id": task_id, "steer_type": steer_type, "queued_at": _time.time()}


# ─── COGNITIVE TOPOLOGY ────────────────────────────────────────────────────────

@router.get("/topology")
async def get_topology(
    tenant_id: str = Query(default="default"),
    limit: int = Query(default=40, le=200),
):
    """Return a graph of nodes/edges representing the cognitive topology:
    agents → tasks → outcomes, plus lessons as knowledge nodes.
    Falls back to a DB-derived graph if Neo4j is unavailable.
    """
    nodes: list[dict] = []
    edges: list[dict] = []

    try:
        async with async_db() as conn:
            async with conn.cursor() as cur:
                # Agent nodes from recent task agents
                await cur.execute(
                    """SELECT agent_name, COUNT(*) AS cnt,
                              SUM(CASE WHEN status='done' THEN 1 ELSE 0 END) AS done,
                              SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) AS errs
                       FROM tasks WHERE tenant_id=%s AND updated_at > NOW()-INTERVAL '7 days'
                       GROUP BY agent_name ORDER BY cnt DESC LIMIT 20""",
                    (tenant_id,),
                )
                agents = await cur.fetchall()
                agent_names = set()
                for a in agents:
                    name = a["agent_name"] or "unknown"
                    agent_names.add(name)
                    nodes.append({
                        "id":    f"agent:{name}",
                        "label": name[:20],
                        "group": "agent",
                        "value": int(a["cnt"]) + 5,
                        "title": f"{name} · {a['cnt']} tasks · {a['done']} ok · {a['errs']} err",
                    })
                # Orchestrator hub
                nodes.insert(0, {"id": "orch", "label": "Orchestrator", "group": "agent", "value": 25, "title": "Central Orchestrator"})
                for name in list(agent_names)[:12]:
                    edges.append({"from": "orch", "to": f"agent:{name}"})

                # Recent task nodes
                await cur.execute(
                    "SELECT id, agent_name, status, description FROM tasks"
                    " WHERE tenant_id=%s ORDER BY updated_at DESC LIMIT %s",
                    (tenant_id, limit),
                )
                tasks = await cur.fetchall()
                for t in tasks:
                    an = t["agent_name"] or "unknown"
                    gp = "success" if t["status"] == "done" else ("failure" if t["status"] == "error" else "task")
                    nodes.append({
                        "id":    f"task:{t['id']}",
                        "label": f"#{t['id']}",
                        "group": gp,
                        "value": 4,
                        "title": (t["description"] or "")[:80],
                    })
                    if an in agent_names:
                        edges.append({"from": f"agent:{an}", "to": f"task:{t['id']}"})

                # Lesson nodes
                await cur.execute(
                    "SELECT id, title, tags FROM lessons WHERE tenant_id=%s ORDER BY created_at DESC LIMIT 15",
                    (tenant_id,),
                )
                for l in await cur.fetchall():
                    nodes.append({"id": f"lesson:{l['id']}", "label": (l["title"] or "lição")[:20], "group": "lesson", "value": 8,
                                  "title": f"Lição #{l['id']}: {l['title']}"})

    except Exception as exc:
        log.debug("topology db error=%s", exc)

    return {"nodes": nodes, "edges": edges, "node_count": len(nodes), "edge_count": len(edges)}


# ─── KNOWLEDGE-ROI endpoint ────────────────────────────────────────────────────

@router.get("/cost/roi")
async def get_cost_roi(tenant_id: str = Query(default="default")):
    """Return per-agent ROI: tasks completed per dollar spent."""
    agents: list[dict] = []
    try:
        async with async_db() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """SELECT agent_name,
                              COUNT(*) FILTER (WHERE status='done') AS tasks_done,
                              COUNT(*) FILTER (WHERE status='error') AS tasks_err,
                              COALESCE(SUM(tokens_used),0) AS tokens,
                              ROUND(COALESCE(SUM(tokens_used),0)::numeric * 0.000002, 6) AS cost_usd
                       FROM tasks
                       WHERE tenant_id=%s AND updated_at > NOW()-INTERVAL '30 days'
                       GROUP BY agent_name ORDER BY tasks_done DESC LIMIT 20""",
                    (tenant_id,),
                )
                for r in await cur.fetchall():
                    cost = float(r["cost_usd"] or 0)
                    done = int(r["tasks_done"] or 0)
                    roi  = round(done / cost, 2) if cost > 0 else None
                    agents.append({
                        "agent":      r["agent_name"] or "unknown",
                        "tasks_done": done,
                        "tasks_err":  int(r["tasks_err"] or 0),
                        "tokens":     int(r["tokens"] or 0),
                        "cost_usd":   cost,
                        "roi":        roi,
                    })
    except Exception as exc:
        log.debug("cost_roi error=%s", exc)
    return {"agents": agents}
