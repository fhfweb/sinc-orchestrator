#!/usr/bin/env python3
"""Sync a markdown projection from task_state_db into ai-system/tasks."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

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


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate ai-system task projection from task_state_db.")
    parser.add_argument("--project-path", required=True, help="Project root path containing ai-orchestrator and ai-system.")
    parser.add_argument(
        "--db-path",
        default="",
        help="Optional sqlite db path. Default: <project>/ai-orchestrator/state/task-state-v3.db",
    )
    parser.add_argument(
        "--output-md",
        default="",
        help="Optional output markdown path. Default: <project>/ai-system/tasks/task-state-projection.md",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Optional output json path. Default: <project>/ai-system/tasks/task-state-projection.json",
    )
    parser.add_argument("--limit", type=int, default=120, help="Max tasks per section in markdown output.")
    parser.add_argument("--emit-json", action="store_true", help="Emit machine-readable summary.")
    return parser.parse_args()


def metadata_get(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    value = row[0]
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(text, encoding="utf-8")
    os.replace(temp_path, path)


def is_open_execution(task: dict[str, Any]) -> bool:
    task_id = str(task.get("task_id") or "").upper()
    status = str(task.get("status") or "").lower()
    if status not in OPEN_STATUSES:
        return False
    return any(task_id.startswith(prefix) for prefix in EXECUTION_PREFIXES)


def markdown_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_none_\n"
    lines = [
        "| Task | Status | Priority | Agent | Updated |",
        "|---|---|---|---|---|",
    ]
    for item in rows:
        task_id = str(item.get("task_id") or "").replace("|", "\\|")
        status = str(item.get("status") or "").replace("|", "\\|")
        priority = str(item.get("priority") or "").replace("|", "\\|")
        agent = str(item.get("assigned_agent") or "").replace("|", "\\|")
        updated = str(item.get("updated_at") or "").replace("|", "\\|")
        lines.append(f"| {task_id} | {status} | {priority} | {agent} | {updated} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    args = parse_args()
    project_path = Path(args.project_path).resolve()
    db_path = Path(args.db_path).resolve() if args.db_path else project_path / "ai-orchestrator" / "state" / "task-state-v3.db"
    output_md = (
        Path(args.output_md).resolve()
        if args.output_md
        else project_path / "ai-system" / "tasks" / "task-state-projection.md"
    )
    output_json = (
        Path(args.output_json).resolve()
        if args.output_json
        else project_path / "ai-system" / "tasks" / "task-state-projection.json"
    )

    if not db_path.exists():
        result = {
            "ok": False,
            "error": f"state db not found: {db_path}",
            "project_path": str(project_path),
            "db_path": str(db_path),
        }
        if args.emit_json:
            print(json.dumps(result, ensure_ascii=True))
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT task_id, title, status, priority, assigned_agent, updated_at, blocked_reason
        FROM tasks
        ORDER BY updated_at DESC, task_id ASC
        """
    ).fetchall()
    tasks = [dict(row) for row in rows]
    status_rows = conn.execute(
        "SELECT status, COUNT(*) AS c FROM tasks GROUP BY status ORDER BY c DESC, status ASC"
    ).fetchall()
    status_counts = {str(r["status"]): int(r["c"]) for r in status_rows}

    backend_mode = metadata_get(conn, "backend_mode", "unknown")
    last_sync_at = metadata_get(conn, "last_sync_at", "")
    scheduler_last_write_at = metadata_get(conn, "scheduler_last_write_at", "")
    last_dag_flush_at = metadata_get(conn, "last_dag_flush_at", "")
    conn.close()

    open_execution = [t for t in tasks if is_open_execution(t)]
    in_progress = [t for t in tasks if str(t.get("status") or "").lower() == "in-progress"]
    blocked = [t for t in tasks if str(t.get("status") or "").lower().startswith("blocked")]
    completed = [t for t in tasks if str(t.get("status") or "").lower() in {"done", "completed"}]

    limit = max(1, int(args.limit))
    projection = {
        "generated_at": utc_now(),
        "project_path": str(project_path),
        "db_path": str(db_path),
        "backend_mode": backend_mode,
        "last_sync_at": last_sync_at,
        "scheduler_last_write_at": scheduler_last_write_at,
        "last_dag_flush_at": last_dag_flush_at,
        "tasks_total": len(tasks),
        "status_counts": status_counts,
        "open_execution_tasks": len(open_execution),
        "in_progress_tasks": len(in_progress),
        "blocked_tasks": len(blocked),
        "completed_tasks": len(completed),
        "sample": {
            "open_execution": open_execution[:limit],
            "in_progress": in_progress[:limit],
            "blocked": blocked[:limit],
            "completed": completed[:limit],
        },
    }

    md_lines = [
        "# Task State Projection",
        "",
        "Generated from `task_state_db` (canonical runtime source).",
        "",
        f"- generated_at: `{projection['generated_at']}`",
        f"- backend_mode: `{backend_mode}`",
        f"- db_path: `{db_path}`",
        f"- tasks_total: `{projection['tasks_total']}`",
        f"- open_execution_tasks: `{projection['open_execution_tasks']}`",
        f"- in_progress_tasks: `{projection['in_progress_tasks']}`",
        f"- blocked_tasks: `{projection['blocked_tasks']}`",
        f"- completed_tasks: `{projection['completed_tasks']}`",
        f"- last_sync_at: `{last_sync_at}`",
        f"- scheduler_last_write_at: `{scheduler_last_write_at}`",
        f"- last_dag_flush_at: `{last_dag_flush_at}`",
        "",
        "## Status Counts",
        "",
    ]
    if status_counts:
        for status_name, count in status_counts.items():
            md_lines.append(f"- `{status_name}`: {count}")
    else:
        md_lines.append("- _none_")

    md_lines.extend(
        [
            "",
            "## In Progress",
            "",
            markdown_table(in_progress[:limit]).rstrip(),
            "",
            "## Open Execution",
            "",
            markdown_table(open_execution[:limit]).rstrip(),
            "",
            "## Blocked",
            "",
            markdown_table(blocked[:limit]).rstrip(),
            "",
            "## Completed (Recent)",
            "",
            markdown_table(completed[:limit]).rstrip(),
            "",
        ]
    )
    markdown = "\n".join(md_lines).strip() + "\n"

    atomic_write_text(output_md, markdown)
    atomic_write_text(output_json, json.dumps(projection, ensure_ascii=False, indent=2) + "\n")

    result = {
        "success": True,
        "project_path": str(project_path),
        "db_path": str(db_path),
        "output_md": str(output_md),
        "output_json": str(output_json),
        "tasks_total": projection["tasks_total"],
        "open_execution_tasks": projection["open_execution_tasks"],
        "blocked_tasks": projection["blocked_tasks"],
    }
    if args.emit_json:
        print(json.dumps(result, ensure_ascii=True))
    else:
        print(
            "[sync_ai_system_projection] ok: tasks={tasks} open_execution={open_exec} output={output}".format(
                tasks=result["tasks_total"],
                open_exec=result["open_execution_tasks"],
                output=result["output_md"],
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
