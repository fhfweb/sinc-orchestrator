#!/usr/bin/env python3
"""
V5 Task State DB (SQLite) helper.

Purpose:
- Keep a relational mirror of ai-orchestrator/tasks/task-dag.json.
- Enable fast/transactional queries without breaking current JSON workflow.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path
from typing import Any

try:
    import psycopg  # type: ignore
except Exception:  # noqa: BLE001
    psycopg = None

DEFAULT_OPEN_STATUSES = (
    "pending",
    "in-progress",
    "blocked",
    "blocked-runtime",
    "blocked-lock-conflict",
    "blocked-phase-approval",
    "blocked-no-agent",
    "blocked-waiting-answers",
    "blocked-startup",
    "needs-revision",
)
DEFAULT_EXECUTION_PREFIXES = ("FEAT-", "DEV-", "TASK-", "REPAIR-", "REFACTOR-", "COBERTURA-", "RECHECK-")
DB_PRIMARY_MODE = "db-primary-v1"
JSON_MIRROR_MODE = "json-mirror-v1"
ROADMAP_PENDING_MARKERS = re.compile(r"(?im)(\[\s\])|\b(pending|todo|to-do|in progress|em andamento)\b")
SUPPORTED_TASK_DB_DRIVERS = {"sqlite", "postgres", "auto"}


def parse_csv_env(name: str, defaults: tuple[str, ...], upper: bool = False) -> tuple[str, ...]:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return defaults
    out: list[str] = []
    seen: set[str] = set()
    for part in raw.split(","):
        text = part.strip()
        if not text:
            continue
        normalized = text.upper() if upper else text.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(normalized)
    return tuple(out) if out else defaults


OPEN_STATUSES = set(parse_csv_env("ORCHESTRATOR_OPEN_TASK_STATUSES", DEFAULT_OPEN_STATUSES, upper=False))
EXECUTION_PREFIXES = tuple(parse_csv_env("ORCHESTRATOR_EXECUTION_TASK_PREFIXES", DEFAULT_EXECUTION_PREFIXES, upper=True))


def parse_bool_env(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "enabled"}


def load_project_task_db_env(project_path: Path) -> None:
    """Load task DB env defaults from project docker env file when shell env is missing."""
    env_candidates = (
        project_path / "ai-orchestrator" / "docker" / ".env.docker.generated",
        project_path / ".env",
    )
    for env_path in env_candidates:
        if not env_path.exists():
            continue
        try:
            lines = env_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            text = line.strip()
            if not text or text.startswith("#") or "=" not in text:
                continue
            key, value = text.split("=", 1)
            key = key.strip()
            if not key or key in os.environ:
                continue
            os.environ[key] = value.strip()
        break


def resolve_task_db_driver() -> str:
    requested = (os.getenv("ORCHESTRATOR_TASK_DB_DRIVER") or "sqlite").strip().lower()
    if requested not in SUPPORTED_TASK_DB_DRIVERS:
        return "sqlite"
    return requested


def build_postgres_dsn() -> str:
    explicit = (os.getenv("ORCHESTRATOR_TASK_DB_DSN") or "").strip()
    if explicit:
        return explicit
    host = (os.getenv("ORCHESTRATOR_TASK_DB_HOST") or "127.0.0.1").strip()
    port = (os.getenv("ORCHESTRATOR_TASK_DB_PORT") or "5432").strip()
    name = (os.getenv("ORCHESTRATOR_TASK_DB_NAME") or "orchestrator_tasks").strip()
    user = (os.getenv("ORCHESTRATOR_TASK_DB_USER") or "orchestrator").strip()
    password = (os.getenv("ORCHESTRATOR_TASK_DB_PASSWORD") or "").strip()
    sslmode = (os.getenv("ORCHESTRATOR_TASK_DB_SSLMODE") or "disable").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"


def build_postgres_fallback_dsn() -> str:
    explicit = (os.getenv("ORCHESTRATOR_TASK_DB_DSN_FALLBACK") or "").strip()
    if explicit:
        return explicit
    host = (os.getenv("ORCHESTRATOR_TASK_DB_HOST_FALLBACK") or "").strip()
    if not host:
        return ""
    port = (os.getenv("ORCHESTRATOR_TASK_DB_PORT_FALLBACK") or os.getenv("ORCHESTRATOR_TASK_DB_PORT") or "5432").strip()
    name = (os.getenv("ORCHESTRATOR_TASK_DB_NAME_FALLBACK") or os.getenv("ORCHESTRATOR_TASK_DB_NAME") or "orchestrator_tasks").strip()
    user = (os.getenv("ORCHESTRATOR_TASK_DB_USER_FALLBACK") or os.getenv("ORCHESTRATOR_TASK_DB_USER") or "orchestrator").strip()
    password = (os.getenv("ORCHESTRATOR_TASK_DB_PASSWORD_FALLBACK") or os.getenv("ORCHESTRATOR_TASK_DB_PASSWORD") or "").strip()
    sslmode = (os.getenv("ORCHESTRATOR_TASK_DB_SSLMODE_FALLBACK") or os.getenv("ORCHESTRATOR_TASK_DB_SSLMODE") or "disable").strip()
    return f"postgresql://{user}:{password}@{host}:{port}/{name}?sslmode={sslmode}"


def mask_postgres_dsn(dsn: str) -> str:
    if not dsn:
        return ""
    return re.sub(r"//([^:/@]+):([^@]+)@", r"//\1:***@", dsn)


def utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def new_sync_id(prefix: str) -> str:
    now = dt.datetime.now(dt.timezone.utc)
    return f"{prefix}-{now.strftime('%Y%m%d%H%M%S%f')}-{uuid.uuid4().hex[:8]}"


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def roadmap_has_pending_items(project_path: Path) -> bool:
    roadmap_candidates = (
        project_path / "ai-orchestrator" / "memory" / "roadmap.md",
        project_path / "ai-orchestrator" / "roadmap.md",
    )
    for roadmap_path in roadmap_candidates:
        if not roadmap_path.exists():
            continue
        try:
            content = roadmap_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if ROADMAP_PENDING_MARKERS.search(content):
            return True
    return False


def detect_execution_backlog_gap(project_path: Path, open_execution_tasks: int) -> bool:
    if open_execution_tasks > 0:
        return False
    return roadmap_has_pending_items(project_path)


def get_task_dependencies(task: dict[str, Any]) -> list[str]:
    raw = task.get("depends_on")
    if raw is None:
        raw = task.get("dependencies")
    if raw is None:
        return []
    if isinstance(raw, str):
        text = raw.strip()
        return [text] if text else []
    out: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            text = as_text(item).strip()
            if text:
                out.append(text)
    return out


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;
        PRAGMA temp_store=MEMORY;

        CREATE TABLE IF NOT EXISTS metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tasks (
            task_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            description TEXT NOT NULL,
            reason TEXT NOT NULL,
            status TEXT NOT NULL,
            priority TEXT NOT NULL,
            assigned_agent TEXT NOT NULL,
            preferred_agent TEXT NOT NULL,
            execution_mode TEXT NOT NULL,
            blocked_reason TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            completed_at TEXT NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dependencies (
            task_id TEXT NOT NULL,
            depends_on TEXT NOT NULL,
            PRIMARY KEY (task_id, depends_on)
        );

        CREATE TABLE IF NOT EXISTS sync_runs (
            sync_id TEXT PRIMARY KEY,
            ran_at TEXT NOT NULL,
            tasks_total INTEGER NOT NULL,
            dag_fingerprint TEXT NOT NULL,
            dag_path TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS whiteboard_entries (
            task_id TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            intention TEXT,
            files_intended TEXT,
            status TEXT NOT NULL,
            announced_at TEXT NOT NULL,
            completed_at TEXT,
            handoff_to TEXT
        );

        CREATE TABLE IF NOT EXISTS incidents (
            incident_id TEXT PRIMARY KEY,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            details TEXT,
            severity TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS lessons_learned (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tenant_id TEXT NOT NULL,
            project_id TEXT NOT NULL,
            error_signature TEXT NOT NULL,
            context TEXT,
            attempted_fix TEXT NOT NULL,
            result TEXT NOT NULL,
            confidence REAL DEFAULT 1.0,
            agent_name TEXT,
            task_id TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
        CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
        CREATE INDEX IF NOT EXISTS idx_tasks_assigned_agent ON tasks(assigned_agent);
        CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
        CREATE INDEX IF NOT EXISTS idx_dep_depends_on ON dependencies(depends_on);
        """
    )


def load_dag(dag_path: Path) -> tuple[dict[str, Any], str]:
    if not dag_path.exists():
        raise FileNotFoundError(f"task-dag.json not found: {dag_path}")

    raw = dag_path.read_bytes()
    try:
        text = raw.decode("utf-8-sig", errors="strict")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"task-dag.json is not valid UTF-8: {exc}") from exc

    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"task-dag.json invalid JSON: {exc}") from exc

    if not isinstance(doc, dict):
        raise RuntimeError("task-dag.json root must be an object.")
    tasks = doc.get("tasks")
    if not isinstance(tasks, list):
        raise RuntimeError("task-dag.json missing tasks[] array.")
    return doc, text


def task_row(task: dict[str, Any]) -> tuple[str, ...]:
    task_id = as_text(task.get("id")).strip()
    if not task_id:
        raise ValueError("task without id")
    return (
        task_id,
        as_text(task.get("title")).strip(),
        as_text(task.get("description")).strip(),
        as_text(task.get("reason")).strip(),
        as_text(task.get("status")).strip(),
        as_text(task.get("priority")).strip(),
        as_text(task.get("assigned_agent")).strip(),
        as_text(task.get("preferred_agent")).strip(),
        as_text(task.get("execution_mode")).strip(),
        as_text(task.get("blocked_reason")).strip(),
        as_text(task.get("created_at")).strip(),
        as_text(task.get("updated_at")).strip(),
        as_text(task.get("completed_at")).strip(),
        json.dumps(task, ensure_ascii=False, separators=(",", ":")),
    )


def metadata_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    value = row[0]
    return as_text(value).strip() or default


def metadata_set(conn: sqlite3.Connection, key: str, value: str, now_iso: str) -> None:
    conn.execute(
        """
        INSERT INTO metadata (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, as_text(value), now_iso),
    )


def compute_open_execution_tasks(conn: sqlite3.Connection) -> int:
    prefix_condition = " OR ".join(["UPPER(task_id) LIKE ?"] * len(EXECUTION_PREFIXES))
    query = (
        "SELECT COUNT(*) AS c FROM tasks WHERE status IN ("
        + ",".join("?" for _ in OPEN_STATUSES)
        + f") AND ({prefix_condition})"
    )
    params = list(OPEN_STATUSES) + [f"{p.upper()}%" for p in EXECUTION_PREFIXES]
    return int(conn.execute(query, params).fetchone()["c"])


def compute_open_repairs(conn: sqlite3.Connection) -> int:
    query = (
        "SELECT COUNT(*) AS c FROM tasks "
        "WHERE status IN (" + ",".join("?" for _ in OPEN_STATUSES) + ") "
        "AND UPPER(task_id) LIKE 'REPAIR-%'"
    )
    return int(conn.execute(query, list(OPEN_STATUSES)).fetchone()["c"])


def compute_status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status ORDER BY c DESC"
    ).fetchall()
    return {as_text(r["status"]): int(r["c"]) for r in rows}


def read_tasks_from_db(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT payload_json FROM tasks ORDER BY task_id ASC"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = as_text(row["payload_json"])
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def write_tasks_to_db(
    project_path: Path,
    db_path: Path,
    tasks: list[dict[str, Any]],
    backend_mode: str = DB_PRIMARY_MODE,
) -> dict[str, Any]:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    now = utc_now_iso()
    sync_id = new_sync_id("write")
    inserted = 0
    with conn:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM dependencies")

        for task_any in tasks:
            if not isinstance(task_any, dict):
                continue
            try:
                row = task_row(task_any)
            except ValueError:
                continue
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id,title,description,reason,status,priority,
                    assigned_agent,preferred_agent,execution_mode,blocked_reason,
                    created_at,updated_at,completed_at,payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            inserted += 1
            task_id = row[0]
            for dep in get_task_dependencies(task_any):
                conn.execute(
                    "INSERT OR IGNORE INTO dependencies (task_id, depends_on) VALUES (?, ?)",
                    (task_id, dep),
                )

        metadata_set(conn, "last_sync_at", now, now)
        metadata_set(conn, "backend_mode", backend_mode, now)
        metadata_set(conn, "tasks_total", str(inserted), now)
        metadata_set(conn, "project_path", str(project_path), now)
        metadata_set(conn, "scheduler_last_write_at", now, now)
        conn.execute(
            "INSERT INTO sync_runs (sync_id, ran_at, tasks_total, dag_fingerprint, dag_path) VALUES (?, ?, ?, ?, ?)",
            (sync_id, now, inserted, metadata_get(conn, "dag_fingerprint", ""), metadata_get(conn, "dag_path", "")),
        )

    status_counts = compute_status_counts(conn)
    open_execution_tasks = compute_open_execution_tasks(conn)
    open_repairs = compute_open_repairs(conn)
    conn.close()
    return {
        "ok": True,
        "mode": "write-tasks",
        "project_path": str(project_path),
        "db_path": str(db_path),
        "synced_at": now,
        "tasks_total": inserted,
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
        "backend_mode": backend_mode,
    }


def atomic_write_text(path: Path, text: str) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def sync_state_db(project_path: Path, db_path: Path) -> dict[str, Any]:
    dag_path = project_path / "ai-orchestrator" / "tasks" / "task-dag.json"
    dag_doc, dag_text = load_dag(dag_path)
    tasks_raw = dag_doc.get("tasks", [])
    dag_fingerprint = hashlib.sha256(dag_text.encode("utf-8")).hexdigest()

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    existing_backend_mode = metadata_get(conn, "backend_mode", JSON_MIRROR_MODE)
    if existing_backend_mode not in {JSON_MIRROR_MODE, DB_PRIMARY_MODE}:
        existing_backend_mode = JSON_MIRROR_MODE

    now = utc_now_iso()
    sync_id = new_sync_id("sync")

    with conn:
        conn.execute("DELETE FROM tasks")
        conn.execute("DELETE FROM dependencies")

        inserted = 0
        for task_any in tasks_raw:
            if not isinstance(task_any, dict):
                continue
            try:
                row = task_row(task_any)
            except ValueError:
                continue
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id,title,description,reason,status,priority,
                    assigned_agent,preferred_agent,execution_mode,blocked_reason,
                    created_at,updated_at,completed_at,payload_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                row,
            )
            inserted += 1

            task_id = row[0]
            for dep in get_task_dependencies(task_any):
                conn.execute(
                    "INSERT OR IGNORE INTO dependencies (task_id, depends_on) VALUES (?, ?)",
                    (task_id, dep),
                )

        for key, value in (
            ("last_sync_at", now),
            ("dag_path", str(dag_path)),
            ("dag_fingerprint", dag_fingerprint),
            ("tasks_total", str(inserted)),
            ("backend_mode", existing_backend_mode),
            ("project_path", str(project_path)),
        ):
            metadata_set(conn, key, value, now)

        conn.execute(
            "INSERT INTO sync_runs (sync_id, ran_at, tasks_total, dag_fingerprint, dag_path) VALUES (?, ?, ?, ?, ?)",
            (sync_id, now, inserted, dag_fingerprint, str(dag_path)),
        )

    status_counts = compute_status_counts(conn)
    open_execution_tasks = compute_open_execution_tasks(conn)
    open_repairs = compute_open_repairs(conn)

    conn.close()

    return {
        "ok": True,
        "mode": "sync",
        "project_path": str(project_path),
        "db_path": str(db_path),
        "dag_path": str(dag_path),
        "dag_fingerprint": dag_fingerprint,
        "synced_at": now,
        "tasks_total": inserted,
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
        "backend_mode": existing_backend_mode,
    }


def query_state_db(db_path: Path, query_name: str, limit: int) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"state db not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)

    rows: list[sqlite3.Row]
    params: list[Any] = []

    if query_name == "open-execution":
        prefix_condition = " OR ".join(["UPPER(task_id) LIKE ?"] * len(EXECUTION_PREFIXES))
        sql = (
            "SELECT task_id, title, status, priority, assigned_agent, updated_at "
            "FROM tasks WHERE status IN ("
            + ",".join("?" for _ in OPEN_STATUSES)
            + f") AND ({prefix_condition}) "
            "ORDER BY priority ASC, updated_at DESC LIMIT ?"
        )
        params = list(OPEN_STATUSES) + [f"{p.upper()}%" for p in EXECUTION_PREFIXES] + [limit]
        rows = conn.execute(sql, params).fetchall()
    elif query_name == "blocked":
        rows = conn.execute(
            """
            SELECT task_id, title, status, blocked_reason, assigned_agent, updated_at
            FROM tasks
            WHERE status LIKE 'blocked%'
            ORDER BY updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    elif query_name == "all-tasks":
        rows = conn.execute(
            """
            SELECT task_id, payload_json, status, priority, updated_at
            FROM tasks
            ORDER BY task_id ASC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
    elif query_name == "all-incidents":
        rows = conn.execute(
            """
            SELECT incident_id, category, title, status, created_at
            FROM incidents
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    elif query_name == "all-lessons":
        rows = conn.execute(
            """
            SELECT id as lesson_id, category, lesson, agent_name, created_at
            FROM lessons_learned
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
        raise ValueError(f"unsupported query: {query_name}")

    if query_name == "all-tasks":
        data: list[dict[str, Any]] = []
        for row in rows:
            payload = as_text(row["payload_json"])
            if not payload:
                continue
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                data.append(parsed)
    else:
        data = [dict(r) for r in rows]
    conn.close()
    return {
        "ok": True,
        "mode": "query",
        "query": query_name,
        "db_path": str(db_path),
        "count": len(data),
        "rows": data,
    }


def status_state_db(project_path: Path, db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {
            "ok": False,
            "mode": "status",
            "project_path": str(project_path),
            "db_path": str(db_path),
            "error": "state-db-missing",
        }

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    status_counts = compute_status_counts(conn)
    tasks_total = int(sum(status_counts.values()))
    open_execution_tasks = compute_open_execution_tasks(conn) if tasks_total > 0 else 0
    open_repairs = compute_open_repairs(conn) if tasks_total > 0 else 0
    backend_mode = metadata_get(conn, "backend_mode", JSON_MIRROR_MODE)
    dag_path = metadata_get(
        conn,
        "dag_path",
        str(project_path / "ai-orchestrator" / "tasks" / "task-dag.json"),
    )
    dag_fingerprint = metadata_get(conn, "dag_fingerprint", "")
    last_sync_at = metadata_get(conn, "last_sync_at", "")
    scheduler_last_write_at = metadata_get(conn, "scheduler_last_write_at", "")
    last_dag_flush_at = metadata_get(conn, "last_dag_flush_at", "")
    conn.close()

    return {
        "ok": True,
        "mode": "status",
        "project_path": str(project_path),
        "db_path": str(db_path),
        "backend_mode": backend_mode,
        "last_sync_at": last_sync_at,
        "scheduler_last_write_at": scheduler_last_write_at,
        "last_dag_flush_at": last_dag_flush_at,
        "dag_path": dag_path,
        "dag_fingerprint": dag_fingerprint,
        "tasks_total": tasks_total,
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
    }


def write_tasks_state_db(project_path: Path, db_path: Path, tasks_json_path: Path) -> dict[str, Any]:
    if not tasks_json_path.exists():
        raise FileNotFoundError(f"tasks json not found: {tasks_json_path}")
    raw = tasks_json_path.read_text(encoding="utf-8-sig")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise RuntimeError("tasks json must be an array of task objects.")
    tasks = [item for item in parsed if isinstance(item, dict)]
    return write_tasks_to_db(
        project_path=project_path,
        db_path=db_path,
        tasks=tasks,
        backend_mode=DB_PRIMARY_MODE,
    )


def flush_dag_from_db(project_path: Path, db_path: Path, dag_path_override: Path | None = None) -> dict[str, Any]:
    if not db_path.exists():
        raise FileNotFoundError(f"state db not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    tasks = read_tasks_from_db(conn)
    backend_mode = metadata_get(conn, "backend_mode", DB_PRIMARY_MODE)
    dag_path_text = metadata_get(
        conn,
        "dag_path",
        str(project_path / "ai-orchestrator" / "tasks" / "task-dag.json"),
    )
    dag_path = dag_path_override if dag_path_override else Path(dag_path_text)
    dag_path.parent.mkdir(parents=True, exist_ok=True)

    doc: dict[str, Any]
    if dag_path.exists():
        try:
            previous = json.loads(dag_path.read_text(encoding="utf-8-sig"))
            doc = previous if isinstance(previous, dict) else {}
        except Exception:
            doc = {}
    else:
        doc = {}

    now = utc_now_iso()
    doc["tasks"] = tasks
    doc["updated_at"] = now
    serialized = json.dumps(doc, ensure_ascii=False, indent=2)
    atomic_write_text(dag_path, serialized + "\n")
    dag_fingerprint = hashlib.sha256((serialized + "\n").encode("utf-8")).hexdigest()

    with conn:
        metadata_set(conn, "dag_path", str(dag_path), now)
        metadata_set(conn, "dag_fingerprint", dag_fingerprint, now)
        metadata_set(conn, "last_dag_flush_at", now, now)
        metadata_set(conn, "backend_mode", backend_mode or DB_PRIMARY_MODE, now)
    status_counts = compute_status_counts(conn)
    open_execution_tasks = compute_open_execution_tasks(conn) if tasks else 0
    open_repairs = compute_open_repairs(conn) if tasks else 0
    conn.close()

    return {
        "ok": True,
        "mode": "flush-dag",
        "project_path": str(project_path),
        "db_path": str(db_path),
        "dag_path": str(dag_path),
        "dag_fingerprint": dag_fingerprint,
        "backend_mode": backend_mode or DB_PRIMARY_MODE,
        "tasks_total": len(tasks),
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
        "flushed_at": now,
    }


def ensure_pg_schema(conn: Any) -> None:
    with conn.cursor() as cur:
        # Core tables
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL,
                priority TEXT NOT NULL,
                assigned_agent TEXT NOT NULL,
                preferred_agent TEXT NOT NULL,
                execution_mode TEXT NOT NULL,
                blocked_reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                payload_json JSONB NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_runs (
                sync_id TEXT PRIMARY KEY,
                ran_at TEXT NOT NULL,
                tasks_total INTEGER NOT NULL,
                dag_fingerprint TEXT NOT NULL,
                dag_path TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS dependencies (
                task_id TEXT NOT NULL,
                depends_on TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (task_id, depends_on)
            );
            CREATE TABLE IF NOT EXISTS whiteboard_entries (
                task_id TEXT PRIMARY KEY,
                agent TEXT NOT NULL,
                intention TEXT,
                files_intended JSONB,
                status TEXT NOT NULL,
                announced_at TIMESTAMPTZ DEFAULT NOW(),
                completed_at TIMESTAMPTZ,
                handoff_to TEXT
            );
            CREATE TABLE IF NOT EXISTS incidents (
                incident_id TEXT PRIMARY KEY,
                category TEXT NOT NULL,
                title TEXT NOT NULL,
                details TEXT,
                severity TEXT,
                status TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS lessons_learned (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                project_id TEXT NOT NULL DEFAULT '',
                error_signature TEXT NOT NULL,
                context TEXT DEFAULT '',
                attempted_fix TEXT NOT NULL,
                result TEXT NOT NULL,
                lesson TEXT DEFAULT '',
                category TEXT DEFAULT '',
                confidence FLOAT DEFAULT 1.0,
                agent_name VARCHAR(100) DEFAULT '',
                task_id VARCHAR(100) DEFAULT '',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

        # Migration: Ensure dependencies.depends_on exists (fixing "column does not exist" errors)
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='dependencies' AND column_name='depends_on'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE dependencies ADD COLUMN depends_on TEXT NOT NULL DEFAULT ''")
            cur.execute("ALTER TABLE dependencies DROP CONSTRAINT IF EXISTS dependencies_pkey")
            cur.execute("ALTER TABLE dependencies ADD PRIMARY KEY (task_id, depends_on)")

        # Migration: Ensure tasks.payload_json is JSONB
        cur.execute("SELECT data_type FROM information_schema.columns WHERE table_name='tasks' AND column_name='payload_json'")
        row = cur.fetchone()
        if row and row[0] == 'text':
             cur.execute("ALTER TABLE tasks ALTER COLUMN payload_json TYPE JSONB USING payload_json::jsonb")

        # Migration: Ensure tasks.task_id exists (supporting legacy "id" column)
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='tasks' AND column_name='id'")
        if cur.fetchone():
             cur.execute("ALTER TABLE tasks RENAME COLUMN id TO task_id")

        # Migration: Ensure lessons_learned has lesson and category (added in MIG-P2-001)
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='lessons_learned' AND column_name='lesson'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE lessons_learned ADD COLUMN lesson TEXT DEFAULT ''")
        cur.execute("SELECT 1 FROM information_schema.columns WHERE table_name='lessons_learned' AND column_name='category'")
        if not cur.fetchone():
            cur.execute("ALTER TABLE lessons_learned ADD COLUMN category TEXT DEFAULT ''")

        # Indices
        cur.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
            CREATE INDEX IF NOT EXISTS idx_tasks_assigned_agent ON tasks(assigned_agent);
            CREATE INDEX IF NOT EXISTS idx_tasks_updated_at ON tasks(updated_at);
            CREATE INDEX IF NOT EXISTS idx_dep_depends_on ON dependencies(depends_on);
            """
        )


def metadata_get_pg(conn: Any, key: str, default: str = "") -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM metadata WHERE key = %s", (key,))
        row = cur.fetchone()
    if not row:
        return default
    value = row[0]
    return as_text(value).strip() or default


def metadata_set_pg(conn: Any, key: str, value: str, now_iso: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO metadata (key, value, updated_at)
            VALUES (%s, %s, %s)
            ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at
            """,
            (key, as_text(value), now_iso),
        )


def compute_open_execution_tasks_pg(conn: Any) -> int:
    patterns = [f"{p.upper()}%" for p in EXECUTION_PREFIXES]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM tasks
            WHERE status = ANY(%s)
              AND UPPER(task_id) LIKE ANY(%s)
            """,
            (list(OPEN_STATUSES), patterns),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def compute_open_repairs_pg(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM tasks
            WHERE status = ANY(%s)
              AND UPPER(task_id) LIKE 'REPAIR-%%'
            """,
            (list(OPEN_STATUSES),),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def compute_status_counts_pg(conn: Any) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) AS c FROM tasks GROUP BY status ORDER BY c DESC")
        rows = cur.fetchall()
    return {as_text(status): int(count) for status, count in rows}


def read_tasks_from_db_pg(conn: Any) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT payload_json::text FROM tasks ORDER BY task_id ASC")
        rows = cur.fetchall()
    out: list[dict[str, Any]] = []
    for row in rows:
        raw = as_text(row[0])
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def write_tasks_to_db_pg(
    project_path: Path,
    dsn: str,
    tasks: list[dict[str, Any]],
    backend_mode: str = DB_PRIMARY_MODE,
) -> dict[str, Any]:
    if psycopg is None:
        raise RuntimeError("postgres-driver-not-installed (pip install psycopg[binary])")

    now = utc_now_iso()
    sync_id = new_sync_id("write")
    inserted = 0
    conn = psycopg.connect(dsn)
    try:
        ensure_pg_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks")
            cur.execute("DELETE FROM dependencies")
            for task_any in tasks:
                if not isinstance(task_any, dict):
                    continue
                try:
                    row = task_row(task_any)
                except ValueError:
                    continue
                cur.execute(
                    """
                    INSERT INTO tasks (
                        task_id,title,description,reason,status,priority,
                        assigned_agent,preferred_agent,execution_mode,blocked_reason,
                        created_at,updated_at,completed_at,payload_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    row,
                )
                inserted += 1
                task_id = row[0]
                for dep in get_task_dependencies(task_any):
                    cur.execute(
                        "INSERT INTO dependencies (task_id, depends_on) VALUES (%s, %s) ON CONFLICT (task_id, depends_on) DO NOTHING",
                        (task_id, dep),
                    )

            metadata_set_pg(conn, "last_sync_at", now, now)
            metadata_set_pg(conn, "backend_mode", backend_mode, now)
            metadata_set_pg(conn, "tasks_total", str(inserted), now)
            metadata_set_pg(conn, "project_path", str(project_path), now)
            metadata_set_pg(conn, "scheduler_last_write_at", now, now)
            cur.execute(
                "INSERT INTO sync_runs (sync_id, ran_at, tasks_total, dag_fingerprint, dag_path) VALUES (%s, %s, %s, %s, %s)",
                (sync_id, now, inserted, metadata_get_pg(conn, "dag_fingerprint", ""), metadata_get_pg(conn, "dag_path", "")),
            )
        conn.commit()

        status_counts = compute_status_counts_pg(conn)
        open_execution_tasks = compute_open_execution_tasks_pg(conn)
        open_repairs = compute_open_repairs_pg(conn)
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": "write-tasks",
        "project_path": str(project_path),
        "db_path": mask_postgres_dsn(dsn),
        "synced_at": now,
        "tasks_total": inserted,
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
        "backend_mode": backend_mode,
    }


def sync_state_db_pg(project_path: Path, dsn: str) -> dict[str, Any]:
    if psycopg is None:
        raise RuntimeError("postgres-driver-not-installed (pip install psycopg[binary])")

    dag_path = project_path / "ai-orchestrator" / "tasks" / "task-dag.json"
    dag_doc, dag_text = load_dag(dag_path)
    tasks_raw = dag_doc.get("tasks", [])
    dag_fingerprint = hashlib.sha256(dag_text.encode("utf-8")).hexdigest()

    now = utc_now_iso()
    sync_id = new_sync_id("sync")
    inserted = 0

    conn = psycopg.connect(dsn)
    try:
        ensure_pg_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DELETE FROM tasks")
            cur.execute("DELETE FROM dependencies")
            for task_any in tasks_raw:
                if not isinstance(task_any, dict):
                    continue
                try:
                    row = task_row(task_any)
                except ValueError:
                    continue
                cur.execute(
                    """
                    INSERT INTO tasks (
                        task_id,title,description,reason,status,priority,
                        assigned_agent,preferred_agent,execution_mode,blocked_reason,
                        created_at,updated_at,completed_at,payload_json
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    row,
                )
                inserted += 1
                task_id = row[0]
                for dep in get_task_dependencies(task_any):
                    cur.execute(
                        "INSERT INTO dependencies (task_id, depends_on) VALUES (%s, %s) ON CONFLICT (task_id, depends_on) DO NOTHING",
                        (task_id, dep),
                    )

            metadata_set_pg(conn, "last_sync_at", now, now)
            metadata_set_pg(conn, "dag_path", str(dag_path), now)
            metadata_set_pg(conn, "dag_fingerprint", dag_fingerprint, now)
            metadata_set_pg(conn, "tasks_total", str(inserted), now)
            metadata_set_pg(conn, "backend_mode", DB_PRIMARY_MODE, now)
            metadata_set_pg(conn, "project_path", str(project_path), now)
            cur.execute(
                "INSERT INTO sync_runs (sync_id, ran_at, tasks_total, dag_fingerprint, dag_path) VALUES (%s, %s, %s, %s, %s)",
                (sync_id, now, inserted, dag_fingerprint, str(dag_path)),
            )
        conn.commit()

        status_counts = compute_status_counts_pg(conn)
        open_execution_tasks = compute_open_execution_tasks_pg(conn)
        open_repairs = compute_open_repairs_pg(conn)
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": "sync",
        "project_path": str(project_path),
        "db_path": mask_postgres_dsn(dsn),
        "dag_path": str(dag_path),
        "dag_fingerprint": dag_fingerprint,
        "synced_at": now,
        "tasks_total": inserted,
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
        "backend_mode": DB_PRIMARY_MODE,
    }


def query_state_db_pg(dsn: str, query_name: str, limit: int) -> dict[str, Any]:
    if psycopg is None:
        raise RuntimeError("postgres-driver-not-installed (pip install psycopg[binary])")

    conn = psycopg.connect(dsn)
    try:
        ensure_pg_schema(conn)
        with conn.cursor() as cur:
            if query_name == "open-execution":
                patterns = [f"{p.upper()}%" for p in EXECUTION_PREFIXES]
                cur.execute(
                    """
                    SELECT task_id, title, status, priority, assigned_agent, updated_at
                    FROM tasks
                    WHERE status = ANY(%s)
                      AND UPPER(task_id) LIKE ANY(%s)
                    ORDER BY priority ASC, updated_at DESC
                    LIMIT %s
                    """,
                    (list(OPEN_STATUSES), patterns, limit),
                )
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            elif query_name == "blocked":
                cur.execute(
                    """
                    SELECT task_id, title, status, blocked_reason, assigned_agent, updated_at
                    FROM tasks
                    WHERE status LIKE 'blocked%%'
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            elif query_name == "all-tasks":
                cur.execute(
                    """
                    SELECT payload_json::text
                    FROM tasks
                    ORDER BY task_id ASC
                    LIMIT %s
                    """,
                    (max(1, limit),),
                )
                rows = []
                for item in cur.fetchall():
                    raw = as_text(item[0])
                    if not raw:
                        continue
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, dict):
                        rows.append(parsed)
            elif query_name == "all-incidents":
                cur.execute(
                    """
                    SELECT incident_id, category, title, status, created_at
                    FROM incidents
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            elif query_name == "all-lessons":
                cur.execute(
                    """
                    SELECT id as lesson_id, category, lesson, agent_name, created_at
                    FROM lessons_learned
                    ORDER BY created_at DESC
                    LIMIT %s
                    """,
                    (limit,),
                )
                cols = [c.name for c in cur.description]
                rows = [dict(zip(cols, row)) for row in cur.fetchall()]
            else:
                raise ValueError(f"unsupported query: {query_name}")
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": "query",
        "query": query_name,
        "db_path": mask_postgres_dsn(dsn),
        "count": len(rows),
        "rows": rows,
    }


def compute_status_counts_pg(conn: Any) -> dict[str, int]:
    with conn.cursor() as cur:
        cur.execute("SELECT status, COUNT(*) FROM tasks GROUP BY status")
        return {row[0]: int(row[1]) for row in cur.fetchall()}


def compute_open_execution_tasks_pg(conn: Any) -> int:
    patterns = [f"{p.upper()}%" for p in EXECUTION_PREFIXES]
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM tasks
            WHERE status = ANY(%s)
              AND UPPER(task_id) LIKE ANY(%s)
            """,
            (list(OPEN_STATUSES), patterns),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def compute_open_repairs_pg(conn: Any) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*) FROM tasks
            WHERE status = ANY(%s)
              AND UPPER(task_id) LIKE 'REPAIR-%%'
            """,
            (list(OPEN_STATUSES),),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 0


def status_state_db_pg(project_path: Path, dsn: str) -> dict[str, Any]:
    if psycopg is None:
        raise RuntimeError("postgres-driver-not-installed (pip install psycopg[binary])")

    conn = psycopg.connect(dsn)
    try:
        ensure_pg_schema(conn)
        status_counts = compute_status_counts_pg(conn)
        tasks_total = int(sum(status_counts.values()))
        open_execution_tasks = compute_open_execution_tasks_pg(conn) if tasks_total > 0 else 0
        open_repairs = compute_open_repairs_pg(conn) if tasks_total > 0 else 0
        backend_mode = metadata_get_pg(conn, "backend_mode", DB_PRIMARY_MODE)
        dag_path = metadata_get_pg(
            conn,
            "dag_path",
            str(project_path / "ai-orchestrator" / "tasks" / "task-dag.json"),
        )
        dag_fingerprint = metadata_get_pg(conn, "dag_fingerprint", "")
        last_sync_at = metadata_get_pg(conn, "last_sync_at", "")
        scheduler_last_write_at = metadata_get_pg(conn, "scheduler_last_write_at", "")
        last_dag_flush_at = metadata_get_pg(conn, "last_dag_flush_at", "")
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": "status",
        "project_path": str(project_path),
        "db_path": mask_postgres_dsn(dsn),
        "backend_mode": backend_mode or DB_PRIMARY_MODE,
        "last_sync_at": last_sync_at,
        "scheduler_last_write_at": scheduler_last_write_at,
        "last_dag_flush_at": last_dag_flush_at,
        "dag_path": dag_path,
        "dag_fingerprint": dag_fingerprint,
        "tasks_total": tasks_total,
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
    }


def write_tasks_state_db_pg(project_path: Path, dsn: str, tasks_json_path: Path) -> dict[str, Any]:
    if not tasks_json_path.exists():
        raise FileNotFoundError(f"tasks json not found: {tasks_json_path}")
    raw = tasks_json_path.read_text(encoding="utf-8-sig")
    parsed = json.loads(raw)
    if not isinstance(parsed, list):
        raise RuntimeError("tasks json must be an array of task objects.")
    tasks = [item for item in parsed if isinstance(item, dict)]
    return write_tasks_to_db_pg(
        project_path=project_path,
        dsn=dsn,
        tasks=tasks,
        backend_mode=DB_PRIMARY_MODE,
    )


def update_single_task_pg(dsn: str, task_payload: dict[str, Any]) -> dict[str, Any]:
    if psycopg is None:
        raise RuntimeError("postgres-driver-not-installed")
    task_id = as_text(task_payload.get("id")).strip()
    if not task_id:
        raise ValueError("task missing id")

    now = utc_now_iso()
    task_payload["updated_at"] = now
    row = task_row(task_payload)

    conn = psycopg.connect(dsn)
    try:
        ensure_pg_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO tasks (
                    task_id,title,description,reason,status,priority,
                    assigned_agent,preferred_agent,execution_mode,blocked_reason,
                    created_at,updated_at,completed_at,payload_json
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb)
                ON CONFLICT (task_id) DO UPDATE SET
                    title=EXCLUDED.title,
                    description=EXCLUDED.description,
                    reason=EXCLUDED.reason,
                    status=EXCLUDED.status,
                    priority=EXCLUDED.priority,
                    assigned_agent=EXCLUDED.assigned_agent,
                    preferred_agent=EXCLUDED.preferred_agent,
                    execution_mode=EXCLUDED.execution_mode,
                    blocked_reason=EXCLUDED.blocked_reason,
                    updated_at=EXCLUDED.updated_at,
                    completed_at=EXCLUDED.completed_at,
                    payload_json=EXCLUDED.payload_json
                """,
                row,
            )
            # Update dependencies
            cur.execute("DELETE FROM dependencies WHERE task_id = %s", (task_id,))
            for dep in get_task_dependencies(task_payload):
                cur.execute(
                    "INSERT INTO dependencies (task_id, depends_on) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                    (task_id, dep),
                )
            metadata_set_pg(conn, "scheduler_last_write_at", now, now)
        conn.commit()
    finally:
        conn.close()

    return {"ok": True, "mode": "patch-task", "task_id": task_id, "updated_at": now}


def update_single_task_sqlite(db_path: Path, task_payload: dict[str, Any]) -> dict[str, Any]:
    task_id = as_text(task_payload.get("id")).strip()
    if not task_id:
        raise ValueError("task missing id")

    now = utc_now_iso()
    task_payload["updated_at"] = now
    row = task_row(task_payload)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    with conn:
        conn.execute(
            """
            INSERT INTO tasks (
                task_id,title,description,reason,status,priority,
                assigned_agent,preferred_agent,execution_mode,blocked_reason,
                created_at,updated_at,completed_at,payload_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                title=excluded.title,
                description=excluded.description,
                reason=excluded.reason,
                status=excluded.status,
                priority=excluded.priority,
                assigned_agent=excluded.assigned_agent,
                preferred_agent=excluded.preferred_agent,
                execution_mode=excluded.execution_mode,
                blocked_reason=excluded.blocked_reason,
                updated_at=excluded.updated_at,
                completed_at=excluded.completed_at,
                payload_json=excluded.payload_json
            """,
            row,
        )
        conn.execute("DELETE FROM dependencies WHERE task_id = ?", (task_id,))
        for dep in get_task_dependencies(task_payload):
            conn.execute(
                "INSERT OR IGNORE INTO dependencies (task_id, depends_on) VALUES (?, ?)",
                (task_id, dep),
            )
        metadata_set(conn, "scheduler_last_write_at", now, now)
    conn.close()
    return {"ok": True, "mode": "patch-task", "task_id": task_id, "updated_at": now}


def flush_dag_from_db_pg(project_path: Path, dsn: str, dag_path_override: Path | None = None) -> dict[str, Any]:
    if psycopg is None:
        raise RuntimeError("postgres-driver-not-installed (pip install psycopg[binary])")

    conn = psycopg.connect(dsn)
    try:
        ensure_pg_schema(conn)
        tasks = read_tasks_from_db_pg(conn)
        backend_mode = metadata_get_pg(conn, "backend_mode", DB_PRIMARY_MODE)
        dag_path_text = metadata_get_pg(
            conn,
            "dag_path",
            str(project_path / "ai-orchestrator" / "tasks" / "task-dag.json"),
        )
        dag_path = dag_path_override if dag_path_override else Path(dag_path_text)
        dag_path.parent.mkdir(parents=True, exist_ok=True)

        if dag_path.exists():
            try:
                previous = json.loads(dag_path.read_text(encoding="utf-8-sig"))
                doc = previous if isinstance(previous, dict) else {}
            except Exception:
                doc = {}
        else:
            doc = {}

        now = utc_now_iso()
        doc["tasks"] = tasks
        doc["updated_at"] = now
        serialized = json.dumps(doc, ensure_ascii=False, indent=2)
        atomic_write_text(dag_path, serialized + "\n")
        dag_fingerprint = hashlib.sha256((serialized + "\n").encode("utf-8")).hexdigest()

        metadata_set_pg(conn, "dag_path", str(dag_path), now)
        metadata_set_pg(conn, "dag_fingerprint", dag_fingerprint, now)
        metadata_set_pg(conn, "last_dag_flush_at", now, now)
        metadata_set_pg(conn, "backend_mode", backend_mode or DB_PRIMARY_MODE, now)
        conn.commit()
        status_counts = compute_status_counts_pg(conn)
        open_execution_tasks = compute_open_execution_tasks_pg(conn) if tasks else 0
        open_repairs = compute_open_repairs_pg(conn) if tasks else 0
    finally:
        conn.close()

    return {
        "ok": True,
        "mode": "flush-dag",
        "project_path": str(project_path),
        "db_path": mask_postgres_dsn(dsn),
        "dag_path": str(dag_path),
        "dag_fingerprint": dag_fingerprint,
        "backend_mode": backend_mode or DB_PRIMARY_MODE,
        "tasks_total": len(tasks),
        "status_counts": status_counts,
        "open_execution_tasks": open_execution_tasks,
        "open_repairs": open_repairs,
        "execution_backlog_gap_detected": detect_execution_backlog_gap(project_path, open_execution_tasks),
        "flushed_at": now,
    }


def write_whiteboard_sqlite(db_path: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    now = utc_now_iso()
    with conn:
        conn.execute("DELETE FROM whiteboard_entries")
        for e in entries:
            files = e.get("files_intended", [])
            files_json = json.dumps(files) if isinstance(files, list) else as_text(files)
            conn.execute(
                """
                INSERT INTO whiteboard_entries (task_id, agent, intention, files_intended, status, announced_at, completed_at, handoff_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    as_text(e.get("task_id")),
                    as_text(e.get("agent")),
                    as_text(e.get("intention")),
                    files_json,
                    as_text(e.get("status")),
                    as_text(e.get("announced_at")),
                    as_text(e.get("completed_at")),
                    as_text(e.get("handoff_to")),
                ),
            )
        metadata_set(conn, "whiteboard_updated_at", now, now)
    conn.close()
    return {"ok": True, "count": len(entries)}


def flush_whiteboard_sqlite(db_path: Path, whiteboard_path: Path) -> dict[str, Any]:
    data = get_whiteboard_sqlite(db_path)
    whiteboard_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(whiteboard_path, serialized + "\n")
    return {"ok": True, "path": str(whiteboard_path), "count": len(data.get("entries", []))}


def write_whiteboard_pg(dsn: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    conn = psycopg.connect(dsn)
    ensure_pg_schema(conn)
    now = utc_now_iso()
    with conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM whiteboard_entries")
            for e in entries:
                files = e.get("files_intended", [])
                cur.execute(
                    """
                    INSERT INTO whiteboard_entries (task_id, agent, intention, files_intended, status, announced_at, completed_at, handoff_to)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        as_text(e.get("task_id")),
                        as_text(e.get("agent")),
                        as_text(e.get("intention")),
                        json.dumps(files),
                        as_text(e.get("status")),
                        as_text(e.get("announced_at")),
                        as_text(e.get("completed_at")),
                        as_text(e.get("handoff_to")),
                    ),
                )
            metadata_set_pg(conn, "whiteboard_updated_at", now, now)
        conn.commit()
    conn.close()
    return {"ok": True, "count": len(entries)}


def flush_whiteboard_pg(dsn: str, whiteboard_path: Path) -> dict[str, Any]:
    data = get_whiteboard_pg(dsn)
    whiteboard_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(data, ensure_ascii=False, indent=2)
    atomic_write_text(whiteboard_path, serialized + "\n")
    return {"ok": True, "path": str(whiteboard_path), "count": len(data.get("entries", []))}


def whiteboard_announce_sqlite(db_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    task_id = as_text(data.get("task_id")).strip()
    if not task_id:
        raise ValueError("task_id required")
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    now = as_text(data.get("announced_at") or utc_now_iso())
    files = data.get("files_intended", [])
    files_json = json.dumps(files) if isinstance(files, list) else as_text(files)
    with conn:
        conn.execute(
            """
            INSERT INTO whiteboard_entries (task_id, agent, intention, files_intended, status, announced_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(task_id) DO UPDATE SET
                agent=excluded.agent, intention=excluded.intention, files_intended=excluded.files_intended,
                status=excluded.status, announced_at=excluded.announced_at
            """,
            (task_id, as_text(data.get("agent")), as_text(data.get("intention")), files_json, "announced", now),
        )
        metadata_set(conn, "whiteboard_updated_at", now, now)
    conn.close()
    return {"ok": True, "task_id": task_id}


def get_whiteboard_sqlite(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return {"entries": [], "updated_at": ""}
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    rows = conn.execute("SELECT * FROM whiteboard_entries").fetchall()
    entries = []
    for r in rows:
        d = dict(r)
        try:
            d["files_intended"] = json.loads(d["files_intended"])
        except Exception:
            pass
        entries.append(d)
    updated_at = metadata_get(conn, "whiteboard_updated_at", "")
    conn.close()
    return {"entries": entries, "updated_at": updated_at}


def record_incident_sqlite(db_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    now = utc_now_iso()
    inc_id = as_text(data.get("incident_id") or data.get("id")).strip()
    if not inc_id:
        # Generate hash-based ID if missing
        fingerprint = f"{data.get('category')}|{data.get('title')}|{now}"
        inc_id = "INC-" + hashlib.sha1(fingerprint.encode()).hexdigest()[:8].upper()

    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    with conn:
        conn.execute(
            """
            INSERT INTO incidents (incident_id, category, title, details, severity, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(incident_id) DO UPDATE SET
                category=excluded.category, title=excluded.title, details=excluded.details,
                severity=excluded.severity, status=excluded.status, updated_at=excluded.updated_at
            """,
            (
                inc_id,
                as_text(data.get("category")),
                as_text(data.get("title")),
                as_text(data.get("details")),
                as_text(data.get("severity")),
                as_text(data.get("status") or "open"),
                as_text(data.get("created_at") or now),
                now,
            ),
        )
    conn.close()
    return {"ok": True, "incident_id": inc_id}


def record_lesson_sqlite(db_path: Path, data: dict[str, Any]) -> dict[str, Any]:
    conn = sqlite3.connect(str(db_path))
    ensure_schema(conn)
    now = utc_now_iso()
    with conn:
        conn.execute(
            """
            INSERT INTO lessons_learned (tenant_id, project_id, error_signature, context, attempted_fix, result, confidence, agent_name, task_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                as_text(data.get("tenant_id") or "default"),
                as_text(data.get("project_id") or ""),
                as_text(data.get("error_signature")),
                as_text(data.get("context")),
                as_text(data.get("attempted_fix")),
                as_text(data.get("result")),
                float(data.get("confidence") or 1.0),
                as_text(data.get("agent_name")),
                as_text(data.get("task_id")),
                as_text(data.get("created_at") or now),
            ),
        )
    conn.close()
    return {"ok": True}


def whiteboard_announce_pg(dsn: str, data: dict[str, Any]) -> dict[str, Any]:
    task_id = as_text(data.get("task_id")).strip()
    if not task_id:
        raise ValueError("task_id required")
    conn = psycopg.connect(dsn)
    ensure_pg_schema(conn)
    now = as_text(data.get("announced_at") or utc_now_iso())
    files = data.get("files_intended", [])
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO whiteboard_entries (task_id, agent, intention, files_intended, status, announced_at)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT(task_id) DO UPDATE SET
                    agent=EXCLUDED.agent, intention=EXCLUDED.intention, files_intended=EXCLUDED.files_intended,
                    status=EXCLUDED.status, announced_at=EXCLUDED.announced_at
                """,
                (task_id, as_text(data.get("agent")), as_text(data.get("intention")), json.dumps(files), "announced", now),
            )
            metadata_set_pg(conn, "whiteboard_updated_at", now, now)
        conn.commit()
    conn.close()
    return {"ok": True, "task_id": task_id}


def get_whiteboard_pg(dsn: str) -> dict[str, Any]:
    conn = psycopg.connect(dsn)
    ensure_pg_schema(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT task_id, agent, intention, files_intended::text, status, announced_at::text, completed_at::text, handoff_to FROM whiteboard_entries")
        cols = [c.name for c in cur.description]
        rows = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            try:
                d["files_intended"] = json.loads(d["files_intended"]) if d["files_intended"] else []
            except Exception:
                pass
            rows.append(d)
    updated_at = metadata_get_pg(conn, "whiteboard_updated_at", "")
    conn.close()
    return {"entries": rows, "updated_at": updated_at}


def record_incident_pg(dsn: str, data: dict[str, Any]) -> dict[str, Any]:
    now = utc_now_iso()
    inc_id = as_text(data.get("incident_id") or data.get("id")).strip()
    if not inc_id:
        # Generate hash-based ID if missing
        fingerprint = f"{data.get('category')}|{data.get('title')}|{now}"
        inc_id = "INC-" + hashlib.sha1(fingerprint.encode()).hexdigest()[:8].upper()

    conn = psycopg.connect(dsn)
    ensure_pg_schema(conn)
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO incidents (incident_id, category, title, details, severity, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (incident_id) DO UPDATE SET
                    category=EXCLUDED.category, title=EXCLUDED.title, details=EXCLUDED.details,
                    severity=EXCLUDED.severity, status=EXCLUDED.status, updated_at=EXCLUDED.updated_at
                """,
                (
                    inc_id,
                    as_text(data.get("category")),
                    as_text(data.get("title")),
                    as_text(data.get("details")),
                    as_text(data.get("severity")),
                    as_text(data.get("status") or "open"),
                    as_text(data.get("created_at") or now),
                    now,
                ),
            )
        conn.commit()
    conn.close()
    return {"ok": True, "incident_id": inc_id}


def record_lesson_pg(dsn: str, data: dict[str, Any]) -> dict[str, Any]:
    conn = psycopg.connect(dsn)
    ensure_pg_schema(conn)
    now = utc_now_iso()
    with conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lessons_learned (tenant_id, project_id, error_signature, context, attempted_fix, result, confidence, agent_name, task_id, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    as_text(data.get("tenant_id") or "default"),
                    as_text(data.get("project_id") or ""),
                    as_text(data.get("error_signature")),
                    as_text(data.get("context")),
                    as_text(data.get("attempted_fix")),
                    as_text(data.get("result")),
                    float(data.get("confidence") or 1.0),
                    as_text(data.get("agent_name")),
                    as_text(data.get("task_id")),
                    as_text(data.get("created_at") or now),
                ),
            )
        conn.commit()
    conn.close()
    return {"ok": True}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Task State DB mirror for orchestrator V5.")
    parser.add_argument("--project-path", required=True, help="Project root containing ai-orchestrator/")
    parser.add_argument(
        "--mode",
        choices=(
            "sync",
            "query",
            "status",
            "write-tasks",
            "flush-dag",
            "patch-task",
            "write-whiteboard",
            "flush-whiteboard",
            "whiteboard-announce",
            "whiteboard-status",
            "record-incident",
            "record-lesson",
        ),
        default="sync",
        help="sync = mirror JSON DAG to sqlite, query/status/write-tasks/flush-dag for db-primary operations",
    )
    parser.add_argument(
        "--db-path",
        default="",
        help="Optional explicit sqlite db path. Default: <project>/ai-orchestrator/state/task-state-v3.db",
    )
    parser.add_argument(
        "--query",
        choices=("open-execution", "blocked", "all-tasks", "all-incidents", "all-lessons"),
        default="open-execution",
        help="Query name when mode=query",
    )
    parser.add_argument(
        "--tasks-json-path",
        default="",
        help="Required for mode=write-tasks, patch-task, whiteboard-announce, record-incident, or record-lesson.",
    )
    parser.add_argument(
        "--dag-path",
        default="",
        help="Optional DAG json path override for mode=flush-dag.",
    )
    parser.add_argument("--limit", type=int, default=20, help="Max rows for query mode")
    parser.add_argument("--emit-json", action="store_true", help="Emit machine-readable JSON")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    project_path = Path(args.project_path).resolve()
    load_project_task_db_env(project_path)

    db_path = Path(args.db_path).resolve() if args.db_path else project_path / "ai-orchestrator" / "state" / "task-state-v3.db"
    requested_driver = resolve_task_db_driver()
    fallback_to_sqlite = parse_bool_env("ORCHESTRATOR_TASK_DB_FALLBACK_SQLITE", True)
    pg_dsn = build_postgres_dsn()
    pg_dsn_fallback = build_postgres_fallback_dsn()
    selected_pg_dsn = pg_dsn
    backend_driver = "sqlite"
    fallback_reason = ""

    if requested_driver in {"postgres", "auto"}:
        try:
            if psycopg is None:
                raise RuntimeError("postgres-driver-not-installed (pip install psycopg[binary])")
            connect_timeout = int((os.getenv("ORCHESTRATOR_TASK_DB_CONNECT_TIMEOUT_SECONDS") or "5").strip() or "5")
            probe_conn = psycopg.connect(pg_dsn, connect_timeout=max(1, connect_timeout))
            try:
                ensure_pg_schema(probe_conn)
                probe_conn.commit()
            finally:
                probe_conn.close()
            backend_driver = "postgres"
        except Exception as exc:  # noqa: BLE001
            primary_error = str(exc)
            if pg_dsn_fallback and pg_dsn_fallback != pg_dsn:
                try:
                    connect_timeout = int((os.getenv("ORCHESTRATOR_TASK_DB_CONNECT_TIMEOUT_SECONDS") or "5").strip() or "5")
                    probe_conn = psycopg.connect(pg_dsn_fallback, connect_timeout=max(1, connect_timeout))
                    try:
                        ensure_pg_schema(probe_conn)
                        probe_conn.commit()
                    finally:
                        probe_conn.close()
                    backend_driver = "postgres"
                    selected_pg_dsn = pg_dsn_fallback
                except Exception as fallback_exc:  # noqa: BLE001
                    if requested_driver == "postgres" and not fallback_to_sqlite:
                        error_result = {
                            "ok": False,
                            "error": f"postgres-backend-unavailable: primary={primary_error}; fallback={fallback_exc}",
                            "mode": args.mode,
                            "project_path": str(project_path),
                        }
                        if args.emit_json:
                            print(json.dumps(error_result, ensure_ascii=True))
                        else:
                            print(f"[task_state_db] ERROR: {error_result['error']}")
                        return 1
                    backend_driver = "sqlite"
                    fallback_reason = f"primary={primary_error}; fallback={fallback_exc}"
            else:
                if requested_driver == "postgres" and not fallback_to_sqlite:
                    error_result = {
                        "ok": False,
                        "error": f"postgres-backend-unavailable: {primary_error}",
                        "mode": args.mode,
                        "project_path": str(project_path),
                    }
                    if args.emit_json:
                        print(json.dumps(error_result, ensure_ascii=True))
                    else:
                        print(f"[task_state_db] ERROR: {error_result['error']}")
                    return 1
                backend_driver = "sqlite"
                fallback_reason = primary_error

    try:
        if backend_driver == "postgres":
            if args.mode == "sync":
                result = sync_state_db_pg(project_path=project_path, dsn=selected_pg_dsn)
            elif args.mode == "status":
                result = status_state_db_pg(project_path=project_path, dsn=selected_pg_dsn)
            elif args.mode == "write-tasks":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required for mode=write-tasks")
                tasks_json_path = Path(args.tasks_json_path).resolve()
                result = write_tasks_state_db_pg(
                    project_path=project_path,
                    dsn=selected_pg_dsn,
                    tasks_json_path=tasks_json_path,
                )
            elif args.mode == "write-whiteboard":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                doc = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                entries = doc.get("entries", []) if isinstance(doc, dict) else []
                result = write_whiteboard_pg(dsn=selected_pg_dsn, entries=entries)
            elif args.mode == "flush-whiteboard":
                wb_path = Path(args.dag_path).resolve() if args.dag_path else project_path / "ai-orchestrator" / "state" / "whiteboard.json"
                result = flush_whiteboard_pg(dsn=selected_pg_dsn, whiteboard_path=wb_path)
            elif args.mode == "whiteboard-announce":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                entry = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                result = whiteboard_announce_pg(dsn=selected_pg_dsn, data=entry)
            elif args.mode == "whiteboard-status":
                result = get_whiteboard_pg(dsn=selected_pg_dsn)
            elif args.mode == "record-incident":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                incident = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                result = record_incident_pg(dsn=selected_pg_dsn, data=incident)
            elif args.mode == "record-lesson":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                lesson = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                result = record_lesson_pg(dsn=selected_pg_dsn, data=lesson)
            elif args.mode == "patch-task":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required for mode=patch-task")
                tasks_json_path = Path(args.tasks_json_path).resolve()
                payload = json.loads(tasks_json_path.read_text(encoding="utf-8-sig"))
                result = update_single_task_pg(dsn=selected_pg_dsn, task_payload=payload)
            elif args.mode == "flush-dag":
                dag_override = Path(args.dag_path).resolve() if args.dag_path else None
                result = flush_dag_from_db_pg(
                    project_path=project_path,
                    dsn=selected_pg_dsn,
                    dag_path_override=dag_override,
                )
            else:
                result = query_state_db_pg(dsn=selected_pg_dsn, query_name=args.query, limit=max(1, int(args.limit)))
        else:
            if args.mode == "sync":
                result = sync_state_db(project_path=project_path, db_path=db_path)
            elif args.mode == "status":
                result = status_state_db(project_path=project_path, db_path=db_path)
            elif args.mode == "write-tasks":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required for mode=write-tasks")
                tasks_json_path = Path(args.tasks_json_path).resolve()
                result = write_tasks_state_db(
                    project_path=project_path,
                    db_path=db_path,
                    tasks_json_path=tasks_json_path,
                )
            elif args.mode == "write-whiteboard":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                doc = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                entries = doc.get("entries", []) if isinstance(doc, dict) else []
                result = write_whiteboard_sqlite(db_path=db_path, entries=entries)
            elif args.mode == "flush-whiteboard":
                wb_path = Path(args.dag_path).resolve() if args.dag_path else project_path / "ai-orchestrator" / "state" / "whiteboard.json"
                result = flush_whiteboard_sqlite(db_path=db_path, whiteboard_path=wb_path)
            elif args.mode == "whiteboard-announce":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                entry = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                result = whiteboard_announce_sqlite(db_path=db_path, data=entry)
            elif args.mode == "whiteboard-status":
                result = get_whiteboard_sqlite(db_path=db_path)
            elif args.mode == "record-incident":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                incident = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                result = record_incident_sqlite(db_path=db_path, data=incident)
            elif args.mode == "record-lesson":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required")
                lesson = json.loads(Path(args.tasks_json_path).read_text(encoding="utf-8-sig"))
                result = record_lesson_sqlite(db_path=db_path, data=lesson)
            elif args.mode == "patch-task":
                if not args.tasks_json_path:
                    raise RuntimeError("--tasks-json-path is required for mode=patch-task")
                tasks_json_path = Path(args.tasks_json_path).resolve()
                payload = json.loads(tasks_json_path.read_text(encoding="utf-8-sig"))
                result = update_single_task_sqlite(db_path=db_path, task_payload=payload)
            elif args.mode == "flush-dag":
                dag_override = Path(args.dag_path).resolve() if args.dag_path else None
                result = flush_dag_from_db(
                    project_path=project_path,
                    db_path=db_path,
                    dag_path_override=dag_override,
                )
            else:
                result = query_state_db(db_path=db_path, query_name=args.query, limit=max(1, int(args.limit)))

        result["storage_backend"] = backend_driver
        result["requested_backend"] = requested_driver
        if backend_driver == "postgres":
            result["postgres_dsn"] = mask_postgres_dsn(selected_pg_dsn)
        if fallback_reason:
            result["fallback_reason"] = fallback_reason
            result["fallback_backend"] = "sqlite"
    except Exception as exc:  # noqa: BLE001
        error_result = {"ok": False, "error": str(exc), "mode": args.mode, "project_path": str(project_path)}
        if args.emit_json:
            print(json.dumps(error_result, ensure_ascii=True))
        else:
            print(f"[task_state_db] ERROR: {exc}")
        return 1

    if args.emit_json:
        print(json.dumps(result, ensure_ascii=True, default=str))
    else:
        if args.mode == "sync":
            print(
                "[task_state_db] sync ok: tasks={tasks} open_execution={open_exec} backlog_gap={gap} db={db}".format(
                    tasks=result.get("tasks_total", 0),
                    open_exec=result.get("open_execution_tasks", 0),
                    gap=result.get("execution_backlog_gap_detected", False),
                    db=result.get("db_path", ""),
                )
            )
        elif args.mode == "status":
            print(
                "[task_state_db] status ok: tasks={tasks} open_execution={open_exec} backlog_gap={gap} mode={backend}".format(
                    tasks=result.get("tasks_total", 0),
                    open_exec=result.get("open_execution_tasks", 0),
                    gap=result.get("execution_backlog_gap_detected", False),
                    backend=result.get("backend_mode", ""),
                )
            )
        elif args.mode == "write-tasks":
            print(
                "[task_state_db] write ok: tasks={tasks} open_execution={open_exec} backend={backend}".format(
                    tasks=result.get("tasks_total", 0),
                    open_exec=result.get("open_execution_tasks", 0),
                    backend=result.get("backend_mode", ""),
                )
            )
        elif args.mode == "flush-dag":
            print(
                "[task_state_db] flush-dag ok: tasks={tasks} dag={dag}".format(
                    tasks=result.get("tasks_total", 0),
                    dag=result.get("dag_path", ""),
                )
            )
        else:
            print(f"[task_state_db] query ok: {result.get('query')} rows={result.get('count')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
