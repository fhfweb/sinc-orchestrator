#!/usr/bin/env python3
"""
Lightweight SSE server for Orchestrator live events.

Endpoints:
- GET /
- GET /health
- GET /events
- GET /gates
- GET /dashboard
- GET /dashboard/state
- GET /dashboard/stream
- GET /tasks
- GET /tasks/<task_id>
- POST /tasks/<task_id>/replay
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Iterator

if os.getenv("ALLOW_LEGACY_START_STREAMING_SERVER", "0") != "1":
    raise SystemExit(
        "Start-StreamingServer.py is deprecated. Use the canonical FastAPI control plane "
        "at ai-orchestrator/services/streaming on port 8765, or set "
        "ALLOW_LEGACY_START_STREAMING_SERVER=1 for temporary migration-only use."
    )

try:
    from dotenv import load_dotenv
except ImportError:
    pass

try:
    from flask import Flask, Response, jsonify, request, send_file
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "Flask is required for Start-StreamingServer.py. Install with: pip install flask"
    ) from exc

try:
    import neo4j
except ImportError:
    pass 

try:
    import mysql.connector
except ImportError:
    pass

try:
    import psycopg
except ImportError:
    psycopg = None

try:
    import psycopg2
except ImportError:
    psycopg2 = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-path", default=".")
    parser.add_argument("--host", default=os.getenv("ORCH_DASHBOARD_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("ORCH_DASHBOARD_PORT", "8765")))
    return parser.parse_args()


def build_paths(project_root: Path) -> dict[str, Path]:
    orchestrator_root = project_root / "ai-orchestrator"
    dashboard_path = None
    for ancestor in [project_root, *project_root.parents]:
        candidate = ancestor / "docs" / "agents" / "dashboard.html"
        if candidate.exists():
            dashboard_path = candidate
            break
    if dashboard_path is None:
        dashboard_path = project_root / "docs" / "agents" / "dashboard.html"
    dashboard_json_path = dashboard_path.parent / "dashboard.json"
    return {
        "project_root": project_root,
        "orchestrator_root": orchestrator_root,
        "events": orchestrator_root / "state" / "stream-events.jsonl",
        "gates": orchestrator_root / "state" / "hitl-gates.json",
        "task_dag": orchestrator_root / "tasks" / "task-dag.json",
        "tasks_preflight_dir": orchestrator_root / "tasks" / "preflight",
        "tasks_completions_dir": orchestrator_root / "tasks" / "completions",
        "tasks_replay_dir": orchestrator_root / "tasks" / "replay",
        "dashboard": dashboard_path,
        "dashboard_json_legacy": dashboard_json_path,
        "commander_dashboard": orchestrator_root / "reports" / "commander-dashboard.json",
        "health_report": orchestrator_root / "state" / "health-report.json",
        "runtime_report": orchestrator_root / "reports" / "runtime-observability-report.json",
    }


def load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def event_stream(events_path: Path) -> Iterator[str]:
    last_pos = 0
    while True:
        if events_path.exists():
            try:
                with events_path.open("r", encoding="utf-8", errors="replace") as handle:
                    handle.seek(last_pos)
                    for raw in handle:
                        line = raw.strip()
                        if not line:
                            continue
                        yield f"data: {line}\n\n"
                    last_pos = handle.tell()
            except FileNotFoundError:
                last_pos = 0
            except Exception as exc:
                err = json.dumps({"event_type": "stream-error", "message": str(exc)})
                yield f"data: {err}\n\n"
        else:
            last_pos = 0
        yield ": ping\n\n"
        time.sleep(1.0)


def build_dashboard_payload(paths: dict[str, Path]) -> dict:
    commander = load_json_file(paths["commander_dashboard"])
    legacy = load_json_file(paths["dashboard_json_legacy"])
    health = load_json_file(paths["health_report"])
    runtime = load_json_file(paths["runtime_report"])

    if commander:
        selected = commander
        source = "commander-dashboard"
    elif legacy:
        selected = legacy
        source = "legacy-dashboard"
    else:
        selected = {}
        source = "none"

    return {
        "source": source,
        "dashboard": selected,
        "health_report": health,
        "runtime_report": runtime,
    }


def dashboard_stream(paths: dict[str, Path], interval_seconds: float = 1.0) -> Iterator[str]:
    last_digest = ""
    while True:
        try:
            payload = build_dashboard_payload(paths)
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            if digest != last_digest:
                yield f"event: dashboard\ndata: {body}\n\n"
                last_digest = digest
        except Exception as exc:
            err = json.dumps({"event_type": "dashboard-stream-error", "message": str(exc)})
            yield f"data: {err}\n\n"

        yield ": ping\n\n"
        time.sleep(interval_seconds)


STATUS_ACTIVE = {"in-progress", "active", "running", "executing", "validating"}
STATUS_QUEUED = {
    "pending",
    "queued",
    "open",
    "needs-revision",
    "blocked-no-agent",
    "blocked-lock-conflict",
    "blocked-phase-approval",
}
STATUS_COMPLETED = {"done", "completed", "skipped"}
STATUS_FAILED = {"failed", "error", "cancelled", "blocked-runtime"}


def normalize_status(raw: Any) -> str:
    text = str(raw or "").strip().lower()
    if not text:
        return "pending"
    if text == "open":
        return "pending"
    return text


def parse_iso(value: Any) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        else:
            parsed = parsed.astimezone(dt.timezone.utc)
        return parsed
    except Exception:
        return None


def iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


class Neo4jEncoder(json.JSONEncoder):
    def default(self, obj):
        import datetime
        if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
            return obj.isoformat()
        # Handle Neo4j specific types by name or attribute
        t_name = type(obj).__name__
        if "DateTime" in t_name or "Date" in t_name or "Time" in t_name:
            if hasattr(obj, "isoformat"):
                return obj.isoformat()
            return str(obj)
        try:
            return str(obj)
        except Exception:
            return super().default(obj)


def _is_running_in_docker() -> bool:
    return os.path.exists("/.dockerenv")


def _normalize_db_engine(raw: str) -> str:
    normalized = (raw or "").strip().lower()
    if normalized in {"pgsql", "postgresql"}:
        return "postgres"
    if normalized in {"mysql", "mariadb"}:
        return "mysql"
    if normalized == "sqlite":
        return "sqlite"
    return normalized or "mysql"


def _normalize_db_host(raw_host: str) -> str:
    host = (raw_host or "").strip()
    if not host:
        return "127.0.0.1"

    if _is_running_in_docker():
        return host

    container_aliases = {"db", "mysql", "postgres", "postgresql", "mariadb"}
    if host.lower() in container_aliases:
        return "127.0.0.1"
    return host


def _env_int(name: str, default: int) -> int:
    raw = str(os.getenv(name, "")).strip()
    if not raw:
        return default
    try:
        return int(raw)
    except Exception:
        return default


def _load_db_config() -> dict[str, Any]:
    engine_raw = os.getenv("DB_CONNECTION") or os.getenv("DB_ENGINE") or "mysql"
    engine = _normalize_db_engine(str(engine_raw))
    default_port = 3306 if engine == "mysql" else 5432
    
    # Try multiple common environment variable names for compatibility
    if _is_running_in_docker():
        # Docker usually uses DB_NAME/DB_USER (from our generated configs)
        database = os.getenv("DB_NAME") or os.getenv("DB_DATABASE") or "sinc"
        username = os.getenv("DB_USER") or os.getenv("DB_USERNAME") or "root"
    else:
        # Host/Laravel usually use DB_DATABASE/DB_USERNAME
        database = os.getenv("DB_DATABASE") or os.getenv("DB_NAME") or "sinc"
        username = os.getenv("DB_USERNAME") or os.getenv("DB_USER") or "root"
    
    return {
        "engine": engine,
        "host": _normalize_db_host(str(os.getenv("DB_HOST", "127.0.0.1"))),
        "port": _env_int("DB_PORT", default_port),
        "database": str(database),
        "user": str(username),
        "password": str(os.getenv("DB_PASSWORD", "")),
        "sqlite_path": str(os.getenv("DB_DATABASE") or os.getenv("DB_SQLITE_PATH") or "database/database.sqlite"),
    }


def _connect_db(config: dict[str, Any]) -> Any:
    engine = str(config.get("engine", "mysql"))
    if engine == "mysql":
        if "mysql" not in globals() or not hasattr(mysql, "connector"):
            raise RuntimeError("mysql-connector-not-installed (pip install mysql-connector-python)")
        return mysql.connector.connect(
            host=str(config.get("host", "127.0.0.1")),
            port=int(config.get("port", 3306)),
            database=str(config.get("database", "")),
            user=str(config.get("user", "")),
            password=str(config.get("password", "")),
            connect_timeout=5,
        )

    if engine == "postgres":
        host = str(config.get("host", "127.0.0.1"))
        port = int(config.get("port", 5432))
        database = str(config.get("database", ""))
        user = str(config.get("user", ""))
        password = str(config.get("password", ""))
        if psycopg is not None:
            return psycopg.connect(
                host=host,
                port=port,
                dbname=database,
                user=user,
                password=password,
                connect_timeout=5,
            )
        if psycopg2 is not None:
            return psycopg2.connect(
                host=host,
                port=port,
                dbname=database,
                user=user,
                password=password,
                connect_timeout=5,
            )
        raise RuntimeError("postgres-driver-not-installed (pip install psycopg[binary] or psycopg2-binary)")

    if engine == "sqlite":
        sqlite_target = str(config.get("sqlite_path", "database/database.sqlite"))
        sqlite_file = Path(sqlite_target)
        if not sqlite_file.is_absolute():
            sqlite_file = Path.cwd() / sqlite_file
        sqlite_file.parent.mkdir(parents=True, exist_ok=True)
        return sqlite3.connect(str(sqlite_file))

    raise RuntimeError(f"unsupported-db-engine:{engine}")


def load_tasks(paths: dict[str, Path]) -> list[dict[str, Any]]:
    dag = load_json_file(paths["task_dag"])
    raw_tasks = dag.get("tasks", []) if isinstance(dag, dict) else []
    out: list[dict[str, Any]] = []
    if isinstance(raw_tasks, list):
        for item in raw_tasks:
            if isinstance(item, dict):
                out.append(item)
    return out


def get_priority_rank(priority: str) -> int:
    normalized = (priority or "").strip().upper()
    if normalized == "P0":
        return 0
    if normalized == "P1":
        return 1
    if normalized == "P2":
        return 2
    return 3


def classify_status(status: str) -> str:
    if status in STATUS_ACTIVE:
        return "active"
    if status in STATUS_COMPLETED:
        return "completed"
    if status in STATUS_FAILED:
        return "failed"
    if status.startswith("blocked"):
        return "blocked"
    if status in STATUS_QUEUED:
        return "queued"
    return "queued"


def build_task_metrics(tasks: list[dict[str, Any]]) -> dict[str, Any]:
    active = 0
    queued = 0
    completed = 0
    failed = 0
    blocked = 0
    subtasks = 0
    status_counts: dict[str, int] = {}

    for task in tasks:
        status = normalize_status(task.get("status"))
        status_counts[status] = status_counts.get(status, 0) + 1
        bucket = classify_status(status)
        if bucket == "active":
            active += 1
        elif bucket == "completed":
            completed += 1
        elif bucket == "failed":
            failed += 1
        elif bucket == "blocked":
            blocked += 1
        else:
            queued += 1

        if any(task.get(key) for key in ("parent_task_id", "parent_id", "parent", "parentTaskId")):
            subtasks += 1

    return {
        "active_tasks": active,
        "queued_tasks": queued,
        "completed_tasks": completed,
        "failed_tasks": failed,
        "blocked_tasks": blocked,
        "subtasks": subtasks,
        "total_tasks": len(tasks),
        "status_counts": status_counts,
    }


def summarize_task(task: dict[str, Any]) -> dict[str, Any]:
    status = normalize_status(task.get("status"))
    created_at = str(task.get("created_at", "") or "")
    started_at = str(task.get("started_at", "") or "")
    updated_at = str(task.get("updated_at", "") or "")
    completed_at = str(task.get("completed_at", "") or "")

    return {
        "id": str(task.get("id", "") or ""),
        "title": str(task.get("title", "") or ""),
        "description": str(task.get("description", "") or ""),
        "status": status,
        "status_bucket": classify_status(status),
        "priority": str(task.get("priority", "P3") or "P3"),
        "assigned_agent": str(task.get("assigned_agent", "") or ""),
        "preferred_agent": str(task.get("preferred_agent", "") or ""),
        "execution_mode": str(task.get("execution_mode", "") or ""),
        "runtime_engine": str(task.get("runtime_engine", "") or ""),
        "parent_task_id": str(task.get("parent_task_id", "") or task.get("parent_id", "") or ""),
        "dependencies": task.get("dependencies", []) if isinstance(task.get("dependencies"), list) else [],
        "files_affected": task.get("files_affected", []) if isinstance(task.get("files_affected"), list) else [],
        "blocked_reason": str(task.get("blocked_reason", "") or ""),
        "retries": int(task.get("retries", 0) or 0),
        "created_at": created_at,
        "started_at": started_at,
        "updated_at": updated_at,
        "completed_at": completed_at,
    }


def sort_tasks(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    status_order = {"active": 0, "queued": 1, "blocked": 2, "failed": 3, "completed": 4}

    def ts_value(task: dict[str, Any]) -> float:
        updated = parse_iso(task.get("updated_at")) or parse_iso(task.get("created_at"))
        if not updated:
            return 0.0
        return updated.timestamp()

    return sorted(
        tasks,
        key=lambda t: (
            status_order.get(str(t.get("status_bucket", "queued")), 9),
            get_priority_rank(str(t.get("priority", "P3"))),
            -ts_value(t),
            str(t.get("id", "")),
        ),
    )


def read_latest_json_by_prefix(directory: Path, prefix: str) -> dict[str, Any]:
    if not directory.exists():
        return {}
    candidates = sorted(
        directory.glob(f"{prefix}-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        parsed = load_json_file(path)
        if parsed:
            return parsed
    return {}


def read_all_json_by_prefix(directory: Path, prefix: str, limit: int = 10) -> list[dict[str, Any]]:
    if not directory.exists():
        return []
    out: list[dict[str, Any]] = []
    candidates = sorted(
        directory.glob(f"{prefix}-*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )[: max(1, limit)]
    for path in candidates:
        parsed = load_json_file(path)
        if isinstance(parsed, dict) and parsed:
            out.append(parsed)
    return out


def resolve_relative_path(project_root: Path, maybe_relative: str) -> Path | None:
    text = (maybe_relative or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate if candidate.exists() else None
    joined = (project_root / text).resolve()
    if joined.exists():
        return joined
    return None


def read_recent_task_events(events_path: Path, task_id: str, limit: int = 40) -> list[dict[str, Any]]:
    if not events_path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    for raw in reversed(lines):
        line = raw.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        if str(parsed.get("task_id", "")).strip() != task_id:
            continue
        out.append(parsed)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def build_task_timeline(task: dict[str, Any], preflight: dict[str, Any], completions: list[dict[str, Any]], events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    timeline: list[dict[str, Any]] = []

    def add(timestamp: Any, event: str, detail: str = "", source: str = "task") -> None:
        text = str(timestamp or "").strip()
        if not text:
            return
        timeline.append(
            {
                "timestamp": text,
                "event": event,
                "detail": detail,
                "source": source,
            }
        )

    add(task.get("created_at"), "task-created", "Task entered DAG")
    add(task.get("started_at"), "execution-started", "Agent execution started")
    add(task.get("updated_at"), "task-updated", "Task state updated")
    add(task.get("completed_at"), "task-completed", "Task marked as completed")

    add(preflight.get("generated_at"), "preflight-generated", "Preflight plan recorded", source="preflight")
    add(preflight.get("timestamp"), "preflight-timestamp", "Preflight timestamp", source="preflight")

    for completion in completions:
        add(
            completion.get("recorded_at") or completion.get("timestamp"),
            "completion-recorded",
            str(completion.get("summary", "") or "Completion payload persisted"),
            source="completion",
        )

    for event in events:
        add(
            event.get("timestamp"),
            str(event.get("event_type", "event") or "event"),
            str(event.get("message", "") or ""),
            source="stream",
        )

    timeline.sort(
        key=lambda e: parse_iso(e.get("timestamp"))
        or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    )
    return timeline


def build_task_detail(paths: dict[str, Path], task: dict[str, Any]) -> dict[str, Any]:
    project_root = paths["project_root"]
    task_id = str(task.get("id", "") or "").strip()
    preflight: dict[str, Any] = {}

    preflight_path = resolve_relative_path(project_root, str(task.get("preflight_path", "") or ""))
    if preflight_path is not None:
        preflight = load_json_file(preflight_path)
    if not preflight:
        preflight = read_latest_json_by_prefix(paths["tasks_preflight_dir"], task_id)

    completions = read_all_json_by_prefix(paths["tasks_completions_dir"], task_id, limit=8)
    latest_completion = completions[0] if completions else {}
    stream_events = read_recent_task_events(paths["events"], task_id=task_id, limit=60)

    source_files = (
        latest_completion.get("source_files")
        if isinstance(latest_completion.get("source_files"), list)
        else (task.get("source_files") if isinstance(task.get("source_files"), list) else [])
    )
    source_modules = (
        latest_completion.get("source_modules")
        if isinstance(latest_completion.get("source_modules"), list)
        else (task.get("source_modules") if isinstance(task.get("source_modules"), list) else [])
    )

    tool_calls = latest_completion.get("tool_calls", []) if isinstance(latest_completion.get("tool_calls"), list) else []
    completion_changes = latest_completion.get("changes", []) if isinstance(latest_completion.get("changes"), list) else []
    task_changes = task.get("completion_payload_changes", []) if isinstance(task.get("completion_payload_changes"), list) else []
    diff_snippets = []
    for change in task_changes + completion_changes:
        text = str(change or "").strip()
        if not text:
            continue
        diff_snippets.append(text[:220])
    diff_snippets = diff_snippets[:20]

    timeline = build_task_timeline(task, preflight, completions, stream_events)
    summary = summarize_task(task)

    return {
        "id": task_id,
        "summary": summary,
        "metadata": {
            "task_id": task_id,
            "parent_task_id": summary.get("parent_task_id", ""),
            "status": summary.get("status", ""),
            "status_bucket": summary.get("status_bucket", ""),
            "priority": summary.get("priority", ""),
            "assigned_agent": summary.get("assigned_agent", ""),
            "preferred_agent": summary.get("preferred_agent", ""),
            "execution_mode": summary.get("execution_mode", ""),
            "runtime_engine": summary.get("runtime_engine", ""),
            "blocked_reason": summary.get("blocked_reason", ""),
            "retries": summary.get("retries", 0),
            "created_at": summary.get("created_at", ""),
            "started_at": summary.get("started_at", ""),
            "updated_at": summary.get("updated_at", ""),
            "completed_at": summary.get("completed_at", ""),
        },
        "context": {
            "files_affected": summary.get("files_affected", []),
            "dependencies": summary.get("dependencies", []),
            "source_files": source_files,
            "source_modules": source_modules,
            "reason": str(task.get("reason", "") or ""),
            "execution_profile_reason": str(task.get("execution_profile_reason", "") or ""),
            "execution_profile_complexity": int(task.get("execution_profile_complexity", 0) or 0),
        },
        "prompt": {
            "objective": str(preflight.get("objective", "") or ""),
            "thought": str(preflight.get("thought", "") or ""),
            "action_plan": preflight.get("action_plan", []) if isinstance(preflight.get("action_plan"), list) else [],
        },
        "tools": tool_calls,
        "timeline": timeline,
        "diff": {
            "snippets": diff_snippets,
            "files_written": latest_completion.get("files_written", []) if isinstance(latest_completion.get("files_written"), list) else [],
        },
        "reasoning": {
            "preflight_thought": str(preflight.get("thought", "") or ""),
            "task_reason": str(task.get("reason", "") or ""),
            "completion_summary": str(latest_completion.get("summary", "") or ""),
        },
        "resource_usage": {
            "tokens_used": int(task.get("tokens_used", 0) or latest_completion.get("tokens_used", 0) or 0),
            "vectors_queried": int(task.get("vectors_queried", 0) or latest_completion.get("vectors_queried", 0) or 0),
            "memory_mb": int(task.get("memory_mb", 0) or latest_completion.get("memory_mb", 0) or 0),
        },
        "artifacts": {
            "preflight": preflight,
            "latest_completion": latest_completion,
            "completions_count": len(completions),
            "stream_events": stream_events,
        },
    }


def sanitize_file_slug(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return "task"
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in text)


def append_stream_event(events_path: Path, event: dict[str, Any]) -> None:
    events_path.parent.mkdir(parents=True, exist_ok=True)
    with events_path.open("a", encoding="utf-8", errors="replace") as handle:
        handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def create_task_replay_scaffold(
    paths: dict[str, Path],
    task: dict[str, Any],
    replay_mode: str,
    target_model: str,
    requested_by: str,
    notes: str,
) -> dict[str, Any]:
    task_id = str(task.get("id", "") or "").strip()
    if not task_id:
        raise RuntimeError("task-id-missing")

    safe_task_id = sanitize_file_slug(task_id)
    replay_dir = paths["tasks_replay_dir"]
    replay_dir.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d%H%M%S")
    file_name = f"{safe_task_id}-replay-{stamp}.json"
    replay_path = replay_dir / file_name

    scaffold = {
        "schema_version": "v5-task-replay-scaffold",
        "generated_at": iso_now(),
        "task_id": task_id,
        "requested_by": requested_by,
        "replay_mode": replay_mode,
        "target_model": target_model,
        "notes": notes,
        "source_task_snapshot": task,
        "execution_intent": {
            "run_now": False,
            "requires_operator_confirmation": True,
            "recommended_entrypoint": "Invoke-UniversalOrchestratorV2.ps1",
        },
        "next_steps": [
            "Review source_task_snapshot and validation requirements.",
            "Decide if replay should keep same context or branch context.",
            "Execute replay via orchestrator command after operator approval.",
        ],
    }
    replay_path.write_text(
        json.dumps(scaffold, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    stream_event = {
        "timestamp": iso_now(),
        "event_type": "task-replay-scaffolded",
        "level": "info",
        "task_id": task_id,
        "message": f"Replay scaffold created at {replay_path.name}",
        "replay_mode": replay_mode,
        "target_model": target_model,
        "requested_by": requested_by,
    }
    append_stream_event(paths["events"], stream_event)

    return {
        "ok": True,
        "task_id": task_id,
        "replay_mode": replay_mode,
        "target_model": target_model,
        "requested_by": requested_by,
        "notes": notes,
        "replay_path": str(replay_path),
        "replay_file": replay_path.name,
        "stream_event": stream_event,
    }


_neo4j_driver: neo4j.Driver | None = None

def get_neo4j_driver() -> neo4j.Driver | None:
    global _neo4j_driver
    if _neo4j_driver is not None:
        try:
            _neo4j_driver.verify_connectivity()
            return _neo4j_driver
        except Exception:
            try:
                _neo4j_driver.close()
            except:
                pass
            _neo4j_driver = None

    # Environment override prioritized, especially for Docker
    uri = os.getenv("NEO4J_URI", "")
    if not uri:
        # Fallback to defaults
        if _check_port_open("localhost", 7688):
            uri = "bolt://localhost:7688"
        else:
            uri = "bolt://localhost:7687"

    user = os.getenv("NEO4J_USER") or os.getenv("NEO4J_USERNAME") or "neo4j"
    password = os.getenv("NEO4J_PASSWORD", "6c887da889bce4c756657f2e2c2f712be66fbcce099cc6de")
    
    try:
        if "neo4j" not in globals():
            return None
        _neo4j_driver = neo4j.GraphDatabase.driver(uri, auth=(user, password))
        return _neo4j_driver
    except Exception:
        return None


def _check_port_open(host: str, port: int) -> bool:
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def _parse_bool(value: Any, default: bool = False) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return default
    return text in {"1", "true", "yes", "on"}


def sanitize_graph_props(data: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}

    def _clean(value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (dt.datetime, dt.date, dt.time)):
            return value.isoformat()
        if isinstance(value, dict):
            return {str(k): _clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_clean(v) for v in value]
        if hasattr(value, "isoformat"):
            try:
                return value.isoformat()
            except Exception:
                pass
        try:
            return str(value)
        except Exception:
            return None

    return {str(key): _clean(val) for key, val in data.items()}


def _graph_record_to_payload(record: Any, nodes: dict[str, dict[str, Any]], edges: list[dict[str, Any]]) -> None:
    n1 = record.get("n")
    n2 = record.get("m")
    rel = record.get("r")
    for node in [n1, n2]:
        if node is None:
            continue
        node_id = str(getattr(node, "element_id", "") or "")
        if not node_id:
            continue
        if node_id in nodes:
            continue
        props = sanitize_graph_props(dict(node))
        labels = list(getattr(node, "labels", []) or [])
        nodes[node_id] = {
            "id": node_id,
            "labels": labels,
            "properties": props,
        }
    if rel is not None and n1 is not None and n2 is not None:
        edges.append(
            {
                "id": str(getattr(rel, "element_id", "") or f"{getattr(n1, 'element_id', '')}-{getattr(n2, 'element_id', '')}"),
                "type": str(getattr(rel, "type", "") or ""),
                "source": str(getattr(n1, "element_id", "") or ""),
                "target": str(getattr(n2, "element_id", "") or ""),
                "properties": sanitize_graph_props(dict(rel)),
            }
        )


def create_app(paths: dict[str, Path]) -> Flask:
    app = Flask(__name__)
    env_mode = (os.getenv("ORCHESTRATOR_ENV", "") or "").strip().lower()
    require_auth = (
        os.getenv("ORCHESTRATOR_CONTROL_PLANE_REQUIRE_AUTH", "").strip().lower() in {"1", "true", "yes", "on"}
        or env_mode in {"prod", "production"}
    )
    control_token = (os.getenv("ORCHESTRATOR_CONTROL_PLANE_TOKEN", "") or "").strip()
    default_role = (os.getenv("ORCHESTRATOR_CONTROL_PLANE_DEFAULT_ROLE", "viewer") or "viewer").strip().lower()
    role_header = os.getenv("ORCHESTRATOR_CONTROL_PLANE_ROLE_HEADER", "X-Control-Role")
    hitl_signing_key = (os.getenv("ORCHESTRATOR_HITL_SIGNING_KEY", "") or "").strip()
    hitl_require_signed_text = (os.getenv("ORCHESTRATOR_HITL_REQUIRE_SIGNED", "") or "").strip().lower()
    if hitl_require_signed_text in {"1", "true", "yes", "on"}:
        hitl_require_signed = True
    elif hitl_require_signed_text in {"0", "false", "no", "off"}:
        hitl_require_signed = False
    else:
        hitl_require_signed = env_mode in {"prod", "production"}

    role_permissions = {
        "viewer": {"health", "read_dashboard", "read_events", "read_gates", "read_tasks", "replay_tasks"},
        "reviewer": {"health", "read_dashboard", "read_events", "read_gates", "resolve_gates", "read_tasks", "replay_tasks"},
        "admin": {"health", "read_dashboard", "read_events", "read_gates", "resolve_gates", "read_tasks", "replay_tasks"},
    }

    def _extract_bearer_token() -> str:
        auth_header = request.headers.get("Authorization", "") or ""
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()
        return ""

    def _get_request_role() -> str:
        role = (request.headers.get(role_header, "") or "").strip().lower()
        if not role:
            role = default_role
        if role not in role_permissions:
            return default_role
        return role

    def _require_permission(permission: str) -> tuple[bool, Any]:
        if require_auth:
            token = _extract_bearer_token()
            if not token and request.args.get("token"):
                token = str(request.args.get("token", "")).strip()
            if not control_token:
                return False, (jsonify({"error": "control-plane-token-not-configured"}), 500)
            if token != control_token:
                return False, (jsonify({"error": "unauthorized"}), 401)
        role = _get_request_role()
        perms = role_permissions.get(role, set())
        if permission not in perms:
            return False, (jsonify({"error": "forbidden", "role": role, "required_permission": permission}), 403)
        return True, None

    def _run_hitl_resolve(gate_id: str, decision: str, decision_by: str, decision_role: str, approval_token: str) -> dict:
        hitl_script = Path(__file__).resolve().with_name("Invoke-HITLGate.ps1")
        if not hitl_script.exists():
            return {"success": False, "error": f"hitl-script-not-found:{hitl_script}"}
        token_value = (approval_token or "").strip()
        if (not token_value) and hitl_signing_key:
            import hmac
            import hashlib

            payload = f"{gate_id}|{decision.lower()}|{decision_by.strip()}|{decision_role.strip().lower()}"
            token_value = hmac.new(
                hitl_signing_key.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
        if hitl_require_signed and not token_value:
            return {"success": False, "error": "hitl-approval-token-required"}

        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(hitl_script),
            "-Mode",
            "resolve",
            "-ProjectPath",
            str(paths["project_root"]),
            "-GateId",
            gate_id,
            "-Decision",
            decision,
            "-DecisionBy",
            decision_by,
            "-DecisionRole",
            decision_role,
            "-ApprovalToken",
            token_value,
            "-EmitJson",
        ]
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(paths["project_root"]),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            return {"success": False, "error": "hitl-resolve-timeout"}
        if proc.returncode != 0:
            return {"success": False, "error": (proc.stderr or proc.stdout or "hitl-resolve-failed").strip()}
        try:
            return json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return {"success": True, "raw": proc.stdout}

    @app.get("/")
    def root():
        return (
            "",
            302,
            {
                "Location": "/dashboard",
                "Cache-Control": "no-store",
            },
        )

    @app.get("/health")
    def health():
        ok, err_resp = _require_permission("health")
        if not ok:
            return err_resp
        return jsonify(
            {
                "ok": True,
                "project_root": str(paths["project_root"]),
                "events_path": str(paths["events"]),
                "gates_path": str(paths["gates"]),
                "auth_required": require_auth,
            }
        )

    @app.get("/events")
    def events():
        ok, err_resp = _require_permission("read_events")
        if not ok:
            return err_resp
        return Response(
            event_stream(paths["events"]),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/gates")
    def gates():
        ok, err_resp = _require_permission("read_gates")
        if not ok:
            return err_resp
        data = load_json_file(paths["gates"])
        if not data:
            data = {"generated_at": "", "gates": []}
        return jsonify(data)

    @app.post("/gates/resolve")
    def resolve_gate():
        ok, err_resp = _require_permission("resolve_gates")
        if not ok:
            return err_resp
        payload = request.get_json(silent=True) or {}
        gate_id = str(payload.get("gate_id", "")).strip()
        decision = str(payload.get("decision", "approve")).strip().lower()
        decision_by = str(payload.get("decision_by", "control-plane")).strip()
        decision_role = str(payload.get("decision_role", "reviewer")).strip()
        approval_token = str(payload.get("approval_token", "")).strip()
        if not gate_id:
            return jsonify({"success": False, "error": "gate_id-required"}), 400
        if decision not in {"approve", "reject"}:
            return jsonify({"success": False, "error": "decision-must-be-approve-or-reject"}), 400
        result = _run_hitl_resolve(
            gate_id=gate_id,
            decision=decision,
            decision_by=decision_by,
            decision_role=decision_role,
            approval_token=approval_token,
        )
        status = 200 if bool(result.get("success", False)) else 400
        return jsonify(result), status

    @app.post("/api/command")
    def api_command():
        ok, err_resp = _require_permission("resolve_gates")
        if not ok:
            return err_resp
        payload = request.get_json(silent=True) or {}
        cmd = str(payload.get("command", "")).strip()
        if not cmd:
            return jsonify({"success": False, "error": "command-required"}), 400
        
        # log command to system-control.json
        cmd_path = paths["orchestrator_root"] / "state" / "system-control.json"
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = load_json_file(cmd_path) if cmd_path.exists() else {}
        except Exception:
            state = {}
        state["last_command"] = cmd
        state["timestamp"] = iso_now()
        cmd_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))

        append_stream_event(paths["events"], {
            "timestamp": iso_now(),
            "event_type": "command_received",
            "level": "info",
            "message": f"Command received: {cmd}"
        })
        return jsonify({"success": True, "command": cmd})

    @app.post("/api/config/confidence")
    def api_config_confidence():
        ok, err_resp = _require_permission("resolve_gates")
        if not ok:
            return err_resp
        payload = request.get_json(silent=True) or {}
        val = payload.get("value", 72)
        try:
            val = int(val)
        except Exception:
            val = 72
            
        cmd_path = paths["orchestrator_root"] / "state" / "system-control.json"
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = load_json_file(cmd_path) if cmd_path.exists() else {}
        except Exception:
            state = {}
        state["confidence_threshold"] = val
        cmd_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        return jsonify({"success": True, "confidence": val})

    @app.post("/api/system/mode")
    def api_system_mode():
        ok, err_resp = _require_permission("resolve_gates")
        if not ok:
            return err_resp
        payload = request.get_json(silent=True) or {}
        mode = str(payload.get("mode", "")).strip()
        
        cmd_path = paths["orchestrator_root"] / "state" / "system-control.json"
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            state = load_json_file(cmd_path) if cmd_path.exists() else {}
        except Exception:
            state = {}
        state["system_mode"] = mode
        state["timestamp"] = iso_now()
        cmd_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        
        append_stream_event(paths["events"], {
            "timestamp": iso_now(),
            "event_type": "system_mode_changed",
            "level": "warn" if mode == "kill" else "info",
            "message": f"System mode changed to: {mode}"
        })
        return jsonify({"success": True, "mode": mode})

    @app.get("/dashboard")
    def dashboard():
        ok, err_resp = _require_permission("read_dashboard")
        if not ok:
            return err_resp
        dashboard_path = paths["dashboard"]
        if dashboard_path.exists():
            return send_file(str(dashboard_path))
        payload = build_dashboard_payload(paths)
        status = payload.get("dashboard", {})
        status_json = json.dumps(status, ensure_ascii=False, indent=2)
        html = f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>SINC Commander Dashboard</title>
  <style>
    body {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background:#0b1020; color:#e6edf3; margin:0; padding:20px; }}
    h1 {{ font-size:20px; margin:0 0 12px 0; }}
    .card {{ background:#11172a; border:1px solid #25304b; border-radius:10px; padding:14px; margin-bottom:12px; }}
    .muted {{ color:#9fb0d0; }}
    pre {{ white-space:pre-wrap; word-break:break-word; margin:0; }}
    a {{ color:#84c5ff; text-decoration:none; }}
  </style>
</head>
<body>
  <h1>Commander Dashboard (Fallback)</h1>
  <div class="card muted">
    Static dashboard file not found at: <code>{dashboard_path}</code><br/>
    Endpoints: <a href="/health">/health</a> | <a href="/dashboard/state">/dashboard/state</a> | <a href="/dashboard/stream">/dashboard/stream</a>
  </div>
  <div class="card">
    <div class="muted" style="margin-bottom:8px;">Live state snapshot</div>
    <pre id="state">{status_json}</pre>
  </div>
  <script>
    async function refreshState() {{
      try {{
        const res = await fetch('/dashboard/state');
        if (!res.ok) return;
        const json = await res.json();
        const dash = json.dashboard || {{}};
        document.getElementById('state').textContent = JSON.stringify(dash, null, 2);
      }} catch (e) {{}}
    }}
    refreshState();
    setInterval(refreshState, 3000);
  </script>
</body>
</html>"""
        return Response(html, mimetype="text/html")

    @app.get("/dashboard/state")
    def dashboard_state():
        ok, err_resp = _require_permission("read_dashboard")
        if not ok:
            return err_resp
        payload = build_dashboard_payload(paths)
        if not payload.get("dashboard"):
            return (
                jsonify(
                    {
                        "error": "dashboard-state-not-found",
                        "paths": {
                            "commander_dashboard": str(paths["commander_dashboard"]),
                            "legacy_dashboard": str(paths["dashboard_json_legacy"]),
                        },
                    }
                ),
                404,
            )
        return jsonify(payload)

    @app.get("/dashboard/stream")
    def dashboard_stream_route():
        ok, err_resp = _require_permission("read_dashboard")
        if not ok:
            return err_resp

        try:
            interval = float(request.args.get("interval", "1.0"))
        except Exception:
            interval = 1.0
        interval = min(max(interval, 0.25), 10.0)

        return Response(
            dashboard_stream(paths, interval_seconds=interval),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/tasks")
    def tasks():
        ok, err_resp = _require_permission("read_tasks")
        if not ok:
            return err_resp

        all_tasks = load_tasks(paths)
        q = str(request.args.get("q", "") or "").strip().lower()
        status_filter_raw = str(request.args.get("status", "") or "").strip().lower()
        agent_filter = str(request.args.get("agent", "") or "").strip().lower()
        model_filter = str(request.args.get("model", "") or "").strip().lower()
        try:
            limit = int(request.args.get("limit", "300"))
        except Exception:
            limit = 300
        limit = min(max(limit, 1), 2000)

        status_filter = {token.strip() for token in status_filter_raw.split(",") if token.strip()} if status_filter_raw else set()

        summarized = [summarize_task(task) for task in all_tasks]
        filtered: list[dict[str, Any]] = []
        for task in summarized:
            task_id = str(task.get("id", "") or "")
            text_blob = " ".join(
                [
                    task_id,
                    str(task.get("title", "") or ""),
                    str(task.get("description", "") or ""),
                    str(task.get("assigned_agent", "") or ""),
                    str(task.get("execution_mode", "") or ""),
                    str(task.get("runtime_engine", "") or ""),
                    str(task.get("status", "") or ""),
                ]
            ).lower()

            if q and q not in text_blob:
                continue
            if status_filter and str(task.get("status", "")).lower() not in status_filter:
                continue
            if agent_filter and agent_filter not in str(task.get("assigned_agent", "")).lower():
                continue
            if model_filter:
                mode_blob = " ".join(
                    [
                        str(task.get("execution_mode", "") or ""),
                        str(task.get("runtime_engine", "") or ""),
                    ]
                ).lower()
                if model_filter not in mode_blob:
                    continue
            filtered.append(task)

        filtered_sorted = sort_tasks(filtered)
        metrics = build_task_metrics(filtered_sorted)
        return jsonify(
            {
                "ok": True,
                "generated_at": iso_now(),
                "query": {
                    "q": q,
                    "status": sorted(status_filter),
                    "agent": agent_filter,
                    "model": model_filter,
                    "limit": limit,
                },
                "metrics": metrics,
                "tasks_total": len(filtered_sorted),
                "tasks": filtered_sorted[:limit],
            }
        )

    @app.get("/tasks/<task_id>")
    def task_detail(task_id: str):
        ok, err_resp = _require_permission("read_tasks")
        if not ok:
            return err_resp

        normalized_task_id = (task_id or "").strip()
        if not normalized_task_id:
            return jsonify({"ok": False, "error": "task-id-required"}), 400

        tasks_list = load_tasks(paths)
        target = None
        for task in tasks_list:
            if str(task.get("id", "") or "").strip() == normalized_task_id:
                target = task
                break
        if target is None:
            return jsonify({"ok": False, "error": "task-not-found", "task_id": normalized_task_id}), 404

        detail = build_task_detail(paths, target)
        return jsonify({"ok": True, "generated_at": iso_now(), "task": detail})

    @app.post("/tasks/<task_id>/replay")
    def task_replay(task_id: str):
        ok, err_resp = _require_permission("replay_tasks")
        if not ok:
            return err_resp

        normalized_task_id = (task_id or "").strip()
        if not normalized_task_id:
            return jsonify({"ok": False, "error": "task-id-required"}), 400

        payload = request.get_json(silent=True) or {}
        replay_mode = str(payload.get("replay_mode", "same-context") or "same-context").strip().lower()
        target_model = str(payload.get("target_model", "") or "").strip()
        requested_by = str(payload.get("requested_by", "commander-ui") or "commander-ui").strip()
        notes = str(payload.get("notes", "") or "").strip()
        allowed_modes = {"same-context", "updated-context", "different-model"}
        if replay_mode not in allowed_modes:
            return jsonify({"ok": False, "error": "invalid-replay-mode", "allowed_modes": sorted(allowed_modes)}), 400
        if replay_mode == "different-model" and not target_model:
            return jsonify({"ok": False, "error": "target-model-required-for-different-model"}), 400

        tasks_list = load_tasks(paths)
        target = None
        for task in tasks_list:
            if str(task.get("id", "") or "").strip() == normalized_task_id:
                target = task
                break
        if target is None:
            return jsonify({"ok": False, "error": "task-not-found", "task_id": normalized_task_id}), 404

        result = create_task_replay_scaffold(
            paths=paths,
            task=target,
            replay_mode=replay_mode,
            target_model=target_model,
            requested_by=requested_by,
            notes=notes,
        )
        return jsonify(result), 201

    @app.get("/graph/raw")
    def graph_raw():
        ok, err_resp = _require_permission("read_dashboard")
        if not ok:
            return err_resp
        driver = get_neo4j_driver()
        if not driver:
            return jsonify({"error": "neo4j-driver-unavailable"}), 503
        
        try:
            limit = int(request.args.get("limit", 400))
        except Exception:
            limit = 400
        limit = max(10, min(limit, 3000))
        project_slug = str(request.args.get("project_slug", "") or "").strip().lower()
        node_type = str(request.args.get("node_type", "") or "").strip().lower()
        rel_type = str(request.args.get("rel_type", "") or "").strip().upper()
        root_id = str(request.args.get("root_id", "") or "").strip()
        include_orphans = _parse_bool(request.args.get("include_orphans", "0"))

        query_mode = "filtered"
        params: dict[str, Any] = {
            "limit": limit,
            "project_slug": project_slug,
            "node_type": node_type,
            "rel_type": rel_type,
        }

        if root_id:
            query_mode = "root-neighborhood"
            query = """
                MATCH (center:MemoryNode {id: $root_id})
                WHERE ($project_slug = '' OR center.project_slug = $project_slug)
                OPTIONAL MATCH (center)-[r]-(m:MemoryNode)
                WHERE ($rel_type = '' OR type(r) = $rel_type)
                  AND ($node_type = '' OR m.node_type = $node_type OR center.node_type = $node_type)
                RETURN center AS n, r, m
                LIMIT $limit
            """
            params["root_id"] = root_id
        else:
            if include_orphans:
                query_mode = "filtered-with-orphans"
                query = """
                    MATCH (n:MemoryNode)
                    WHERE ($project_slug = '' OR n.project_slug = $project_slug)
                      AND ($node_type = '' OR n.node_type = $node_type)
                    OPTIONAL MATCH (n)-[r]-(m:MemoryNode)
                    WHERE ($rel_type = '' OR type(r) = $rel_type)
                    RETURN n, r, m
                    LIMIT $limit
                """
            else:
                query = """
                    MATCH (n:MemoryNode)-[r]-(m:MemoryNode)
                    WHERE ($project_slug = '' OR n.project_slug = $project_slug OR m.project_slug = $project_slug)
                      AND ($node_type = '' OR n.node_type = $node_type OR m.node_type = $node_type)
                      AND ($rel_type = '' OR type(r) = $rel_type)
                    RETURN n, r, m
                    LIMIT $limit
                """
        try:
            with driver.session() as session:
                result = session.run(query, **params)
                nodes = {}
                edges = []
                for record in result:
                    _graph_record_to_payload(record, nodes, edges)

                # Deduplicate edges by id (some optional matches may generate duplicates)
                dedup_edges: dict[str, dict[str, Any]] = {}
                for edge in edges:
                    edge_id = str(edge.get("id", "") or "")
                    if not edge_id:
                        edge_id = f"{edge.get('source', '')}:{edge.get('type', '')}:{edge.get('target', '')}"
                        edge["id"] = edge_id
                    dedup_edges[edge_id] = edge

                return jsonify(
                    {
                        "ok": True,
                        "mode": query_mode,
                        "filters": {
                            "project_slug": project_slug,
                            "node_type": node_type,
                            "rel_type": rel_type,
                            "root_id": root_id,
                            "limit": limit,
                            "include_orphans": include_orphans,
                        },
                        "nodes": list(nodes.values()),
                        "edges": list(dedup_edges.values()),
                    }
                )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            driver.close()

    @app.get("/graph/meta")
    def graph_meta():
        ok, err_resp = _require_permission("read_dashboard")
        if not ok:
            return err_resp

        driver = get_neo4j_driver()
        if not driver:
            return jsonify({"error": "neo4j-driver-unavailable"}), 503

        try:
            with driver.session() as session:
                project_rows = session.run(
                    """
                    MATCH (n:MemoryNode)
                    WHERE n.project_slug IS NOT NULL AND n.project_slug <> ''
                    RETURN n.project_slug AS value, count(*) AS total
                    ORDER BY total DESC, value ASC
                    LIMIT 100
                    """
                )
                node_type_rows = session.run(
                    """
                    MATCH (n:MemoryNode)
                    WHERE n.node_type IS NOT NULL AND n.node_type <> ''
                    RETURN n.node_type AS value, count(*) AS total
                    ORDER BY total DESC, value ASC
                    LIMIT 200
                    """
                )
                rel_rows = session.run(
                    """
                    MATCH ()-[r]-()
                    RETURN type(r) AS value, count(*) AS total
                    ORDER BY total DESC, value ASC
                    LIMIT 200
                    """
                )
                totals = session.run(
                    """
                    MATCH (n:MemoryNode) WITH count(n) AS nodes
                    MATCH ()-[r]-() RETURN nodes, count(r) AS edges
                    """
                ).single()

                projects = [dict(row) for row in project_rows]
                node_types = [dict(row) for row in node_type_rows]
                relation_types = [dict(row) for row in rel_rows]
                total_nodes = int((totals.get("nodes") if totals else 0) or 0)
                total_edges = int((totals.get("edges") if totals else 0) or 0)

                return jsonify(
                    {
                        "ok": True,
                        "generated_at": iso_now(),
                        "total_nodes": total_nodes,
                        "total_edges": total_edges,
                        "project_slugs": projects,
                        "node_types": node_types,
                        "relation_types": relation_types,
                    }
                )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            driver.close()

    @app.get("/graph/expand")
    def graph_expand():
        ok, err_resp = _require_permission("read_dashboard")
        if not ok:
            return err_resp

        driver = get_neo4j_driver()
        if not driver:
            return jsonify({"error": "neo4j-driver-unavailable"}), 503

        node_id = str(request.args.get("node_id", "") or "").strip()
        if not node_id:
            return jsonify({"error": "node_id-required"}), 400

        try:
            hops = int(request.args.get("hops", "1"))
        except Exception:
            hops = 1
        hops = 1 if hops <= 1 else 2

        try:
            limit = int(request.args.get("limit", "300"))
        except Exception:
            limit = 300
        limit = max(10, min(limit, 2000))

        project_slug = str(request.args.get("project_slug", "") or "").strip().lower()
        node_type = str(request.args.get("node_type", "") or "").strip().lower()
        rel_type = str(request.args.get("rel_type", "") or "").strip().upper()

        params = {
            "node_id": node_id,
            "project_slug": project_slug,
            "node_type": node_type,
            "rel_type": rel_type,
            "limit": limit,
        }

        q_hop_1 = """
            MATCH (center:MemoryNode {id: $node_id})
            WHERE ($project_slug = '' OR center.project_slug = $project_slug)
            OPTIONAL MATCH (center)-[r]-(m:MemoryNode)
            WHERE m IS NULL
               OR (
                    ($project_slug = '' OR m.project_slug = $project_slug)
                AND ($node_type = '' OR m.node_type = $node_type)
                AND ($rel_type = '' OR type(r) = $rel_type)
               )
            RETURN center AS n, r, m
            LIMIT $limit
        """

        q_hop_2 = """
            CALL () {
                MATCH (center:MemoryNode {id: $node_id})
                WHERE ($project_slug = '' OR center.project_slug = $project_slug)
                OPTIONAL MATCH (center)-[r]-(m:MemoryNode)
                WHERE m IS NULL
                   OR (
                        ($project_slug = '' OR m.project_slug = $project_slug)
                    AND ($node_type = '' OR m.node_type = $node_type)
                    AND ($rel_type = '' OR type(r) = $rel_type)
                   )
                RETURN center AS n, r, m
                UNION
                MATCH (center:MemoryNode {id: $node_id})
                WHERE ($project_slug = '' OR center.project_slug = $project_slug)
                MATCH (center)-[r1]-(n1:MemoryNode)
                WHERE ($project_slug = '' OR n1.project_slug = $project_slug)
                  AND ($node_type = '' OR n1.node_type = $node_type)
                MATCH (n1)-[r2]-(n2:MemoryNode)
                WHERE ($project_slug = '' OR n2.project_slug = $project_slug)
                  AND ($node_type = '' OR n2.node_type = $node_type)
                  AND ($rel_type = '' OR type(r2) = $rel_type)
                RETURN n1 AS n, r2 AS r, n2 AS m
            }
            RETURN n, r, m
            LIMIT $limit
        """

        query = q_hop_1 if hops == 1 else q_hop_2

        try:
            with driver.session() as session:
                # Validate root existence quickly.
                root = session.run(
                    """
                    MATCH (n:MemoryNode {id: $node_id})
                    WHERE ($project_slug = '' OR n.project_slug = $project_slug)
                    RETURN n LIMIT 1
                    """,
                    node_id=node_id,
                    project_slug=project_slug,
                ).single()
                if root is None:
                    return jsonify(
                        {
                            "ok": False,
                            "error": "node-not-found",
                            "node_id": node_id,
                            "project_slug": project_slug,
                        }
                    ), 404

                result = session.run(query, **params)
                nodes: dict[str, dict[str, Any]] = {}
                edges: list[dict[str, Any]] = []
                for record in result:
                    _graph_record_to_payload(record, nodes, edges)

                dedup_edges: dict[str, dict[str, Any]] = {}
                for edge in edges:
                    edge_id = str(edge.get("id", "") or "")
                    if not edge_id:
                        edge_id = f"{edge.get('source', '')}:{edge.get('type', '')}:{edge.get('target', '')}"
                        edge["id"] = edge_id
                    dedup_edges[edge_id] = edge

                return Response(
                    json.dumps(
                        {
                            "ok": True,
                            "mode": "expand",
                            "node_id": node_id,
                            "hops": hops,
                            "filters": {
                                "project_slug": project_slug,
                                "node_type": node_type,
                                "rel_type": rel_type,
                                "limit": limit,
                            },
                            "nodes": list(nodes.values()),
                            "edges": list(dedup_edges.values()),
                        },
                        cls=Neo4jEncoder,
                        ensure_ascii=False,
                        separators=(",", ":")
                    ),
                    mimetype="application/json"
                )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            driver.close()

    @app.post("/graph/cypher")
    def graph_cypher():
        ok, err_resp = _require_permission("admin")
        if not ok:
            return err_resp
        payload = request.get_json(silent=True) or {}
        query = str(payload.get("query", "")).strip()
        if not query:
            return jsonify({"error": "query-required"}), 400
        
        driver = get_neo4j_driver()
        if not driver:
            return jsonify({"error": "neo4j-driver-unavailable"}), 503
        
        try:
            with driver.session() as session:
                result = session.run(query)
                res_data = {"ok": True, "data": [dict(r) for r in result]}
                return Response(
                    json.dumps(res_data, cls=Neo4jEncoder, ensure_ascii=False, separators=(",", ":")),
                    mimetype="application/json"
                )
        except Exception as exc:
            return jsonify({"error": str(exc)}), 500
        finally:
            driver.close()


    return app


def main() -> int:
    args = parse_args()
    project_root = Path(args.project_path).resolve()
    if not project_root.exists():
        raise SystemExit(f"Project path not found: {project_root}")
    
    # Load .env files for SINC/integrated context (skip if in Docker to prevent overrides)
    if "load_dotenv" in globals() and not _is_running_in_docker():
        env_paths = [
            project_root / "ai-orchestrator" / "docker" / ".env.docker.generated",
            project_root / ".env"
        ]
        for ep in env_paths:
            if ep.exists():
                load_dotenv(ep, override=True)

    paths = build_paths(project_root)
    app = create_app(paths)
    app.run(host=args.host, port=args.port, debug=False, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
