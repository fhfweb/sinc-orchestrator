"""
SINC Orchestrator File Conflict Detector v2
Detects when multiple agents have overlapping file_intended declarations
on the whiteboard, preventing silent concurrent modifications.

Usage:
    python conflict_detector.py [--mode report|block|resolve]
    python conflict_detector.py --check-before-dispatch TASK_ID AGENT FILES...
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg
import psycopg.rows

BASE       = Path(__file__).parent.parent.parent
WHITEBOARD = BASE / "state" / "whiteboard.json"
LOGS_DIR   = BASE / "logs"

DB_CONFIG = {
    "dbname":   os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":     os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", ""),
    "host":     os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":     os.environ.get("ORCH_DB_PORT",     "5434"),
}


def _db():
    return psycopg.connect(**DB_CONFIG, row_factory=psycopg.rows.dict_row)


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _log(msg: str):
    print(f"[conflict-detector] {msg}")
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "conflict_detector.log", "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] {msg}\n")
    except Exception:
        pass


def _read_whiteboard() -> dict:
    try:
        return json.loads(WHITEBOARD.read_text(encoding="utf-8"))
    except Exception:
        return {"entries": []}


def _normalize_file_path(path: str) -> str:
    """Normalize file path for comparison (lowercase, forward slashes, strip leading /)."""
    return path.lower().replace("\\", "/").lstrip("/").rstrip("/")


def _paths_overlap(path_a: str, path_b: str) -> bool:
    """Check if two file paths overlap (one is a prefix of the other or equal)."""
    a = _normalize_file_path(path_a)
    b = _normalize_file_path(path_b)
    if a == b:
        return True
    # Check if one is a directory prefix of the other
    if a.endswith("*"):
        prefix = a.rstrip("*").rstrip("/")
        return b.startswith(prefix)
    if b.endswith("*"):
        prefix = b.rstrip("*").rstrip("/")
        return a.startswith(prefix)
    # Check if one is a directory that contains the other
    return b.startswith(a + "/") or a.startswith(b + "/")


def detect_conflicts(entries: list[dict]) -> list[dict]:
    """
    Detect file conflicts between active whiteboard entries.
    Returns list of conflict objects.
    """
    active = [
        e for e in entries
        if e.get("status") == "announced" and e.get("files_intended")
    ]

    conflicts = []
    seen_pairs = set()

    for i, entry_a in enumerate(active):
        for j, entry_b in enumerate(active):
            if i >= j:
                continue
            pair_key = tuple(sorted([entry_a["task_id"], entry_b["task_id"]]))
            if pair_key in seen_pairs:
                continue

            overlapping_files = []
            for file_a in entry_a["files_intended"]:
                for file_b in entry_b["files_intended"]:
                    if _paths_overlap(file_a, file_b):
                        overlapping_files.append({
                            "file_a": file_a,
                            "file_b": file_b,
                            "normalized_a": _normalize_file_path(file_a),
                            "normalized_b": _normalize_file_path(file_b),
                        })

            if overlapping_files:
                seen_pairs.add(pair_key)
                severity = "high" if any(
                    "controller" in f["normalized_a"].lower() or
                    "service" in f["normalized_a"].lower() or
                    "model" in f["normalized_a"].lower()
                    for f in overlapping_files
                ) else "medium"

                conflicts.append({
                    "task_a":          entry_a["task_id"],
                    "agent_a":         entry_a.get("agent", "?"),
                    "task_b":          entry_b["task_id"],
                    "agent_b":         entry_b.get("agent", "?"),
                    "overlapping_files": overlapping_files,
                    "severity":        severity,
                    "detected_at":     _now_iso(),
                    "recommendation":  _recommend_resolution(entry_a, entry_b, overlapping_files),
                })

    return conflicts


def _recommend_resolution(entry_a: dict, entry_b: dict, overlapping: list[dict]) -> str:
    """Suggest how to resolve the conflict."""
    files_str = ", ".join(o["file_a"] for o in overlapping[:3])
    return (
        f"Tasks {entry_a['task_id']} ({entry_a.get('agent')}) and "
        f"{entry_b['task_id']} ({entry_b.get('agent')}) both declare: {files_str}. "
        f"Serialize execution: complete {entry_a['task_id']} before starting {entry_b['task_id']}, "
        f"or split responsibilities to avoid shared files."
    )


def check_before_dispatch(task_id: str, agent: str, files: list[str]) -> tuple[bool, list[dict]]:
    """
    Check if dispatching task_id with agent would create conflicts.
    Returns (safe_to_dispatch, conflicts).
    """
    wb = _read_whiteboard()
    active = [e for e in wb.get("entries", []) if e.get("status") == "announced"]

    candidate_entry = {
        "task_id":       task_id,
        "agent":         agent,
        "files_intended": files,
        "status":        "announced",
    }
    conflicts = detect_conflicts(active + [candidate_entry])
    new_conflicts = [c for c in conflicts if task_id in (c["task_a"], c["task_b"])]
    return len(new_conflicts) == 0, new_conflicts


def report_mode() -> list[dict]:
    """Report all current file conflicts on the whiteboard."""
    wb = _read_whiteboard()
    entries = wb.get("entries", [])
    conflicts = detect_conflicts(entries)

    if not conflicts:
        _log("No file conflicts detected on whiteboard.")
        return []

    _log(f"Found {len(conflicts)} file conflict(s):")
    for c in conflicts:
        _log(f"  [{c['severity'].upper()}] {c['task_a']} vs {c['task_b']}")
        for f in c["overlapping_files"][:3]:
            _log(f"    - {f['file_a']} ↔ {f['file_b']}")
        _log(f"    → {c['recommendation']}")

    # Write conflict report to state
    report_path = BASE / "state" / "file-conflicts.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": _now_iso(),
            "total_conflicts": len(conflicts),
            "conflicts": conflicts
        }, f, indent=2)
    _log(f"Report written to {report_path}")
    return conflicts


def block_mode() -> int:
    """
    Block mode: suspend the lower-priority task in each conflict pair.
    Updates the DB to reset the lower-priority task to pending.
    """
    conflicts = report_mode()
    blocked = 0

    try:
        with _db() as conn:
            with conn.cursor() as cur:
                for conflict in conflicts:
                    if conflict["severity"] != "high":
                        continue

                    # Determine which task to block (lower priority or later started)
                    cur.execute(
                        "SELECT id, priority, started_at FROM tasks WHERE id IN (%s, %s)",
                        (conflict["task_a"], conflict["task_b"])
                    )
                    tasks = {r["id"]: r for r in cur.fetchall()}

                    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
                    task_a_pri = priority_order.get(tasks.get(conflict["task_a"], {}).get("priority", "P2"), 2)
                    task_b_pri = priority_order.get(tasks.get(conflict["task_b"], {}).get("priority", "P2"), 2)

                    # Block the lower priority task (higher number = lower priority)
                    task_to_block = conflict["task_b"] if task_a_pri <= task_b_pri else conflict["task_a"]

                    _log(f"  BLOCKING {task_to_block} due to file conflict with "
                         f"{'task_a' if task_to_block == conflict['task_b'] else 'task_b'}")

                    cur.execute("""
                        UPDATE tasks
                        SET status = 'pending', assigned_agent = NULL,
                            updated_at = NOW()
                        WHERE id = %s AND status = 'in-progress'
                    """, (task_to_block,))

                    if cur.rowcount:
                        cur.execute("""
                            INSERT INTO agent_events (task_id, agent_name, event_type, payload)
                            VALUES (%s, %s, 'repair', %s)
                        """, (task_to_block, "conflict-detector", json.dumps({
                            "reason": "file_conflict",
                            "conflicting_task": conflict["task_a"] if task_to_block == conflict["task_b"] else conflict["task_b"],
                            "overlapping_files": conflict["overlapping_files"][:3],
                        })))
                        blocked += 1

                conn.commit()
    except Exception as e:
        _log(f"ERROR in block mode: {e}")

    _log(f"Blocked {blocked} tasks due to high-severity file conflicts.")
    return blocked


def resolve_mode() -> int:
    """
    Resolve mode: add dependency constraints to the DB so the lower-priority
    task must wait for the higher-priority one to complete.
    """
    conflicts = report_mode()
    resolved = 0

    try:
        with _db() as conn:
            with conn.cursor() as cur:
                for conflict in conflicts:
                    priority_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
                    cur.execute(
                        "SELECT id, priority FROM tasks WHERE id IN (%s, %s)",
                        (conflict["task_a"], conflict["task_b"])
                    )
                    tasks = {r["id"]: r for r in cur.fetchall()}

                    task_a_pri = priority_order.get(tasks.get(conflict["task_a"], {}).get("priority", "P2"), 2)
                    task_b_pri = priority_order.get(tasks.get(conflict["task_b"], {}).get("priority", "P2"), 2)

                    # Lower priority task depends on higher priority task completing
                    if task_a_pri <= task_b_pri:
                        dependent, dependency = conflict["task_b"], conflict["task_a"]
                    else:
                        dependent, dependency = conflict["task_a"], conflict["task_b"]

                    cur.execute("""
                        INSERT INTO dependencies (task_id, dependency_id)
                        VALUES (%s, %s)
                        ON CONFLICT DO NOTHING
                    """, (dependent, dependency))

                    if cur.rowcount:
                        _log(f"  DEPENDENCY: {dependent} must wait for {dependency}")
                        resolved += 1

                conn.commit()
    except Exception as e:
        _log(f"ERROR in resolve mode: {e}")

    _log(f"Added {resolved} dependency constraints to resolve conflicts.")
    return resolved


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC File Conflict Detector v2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--mode", choices=["report", "block", "resolve"],
                       help="report=show conflicts, block=suspend lower-priority task, "
                            "resolve=add DB dependencies")
    group.add_argument("--check-before-dispatch", metavar="TASK_ID",
                       help="Check if dispatching this task would create conflicts")
    parser.add_argument("--agent", help="Agent name (for --check-before-dispatch)")
    parser.add_argument("--files", nargs="+", help="Files to check (for --check-before-dispatch)")
    args = parser.parse_args()

    if args.check_before_dispatch:
        safe, conflicts = check_before_dispatch(
            args.check_before_dispatch,
            args.agent or "unknown",
            args.files or []
        )
        if safe:
            _log(f"SAFE to dispatch {args.check_before_dispatch}")
            sys.exit(0)
        else:
            _log(f"CONFLICT detected for {args.check_before_dispatch}:")
            for c in conflicts:
                _log(f"  {c['recommendation']}")
            sys.exit(1)
    elif args.mode == "report":
        report_mode()
    elif args.mode == "block":
        block_mode()
    elif args.mode == "resolve":
        resolve_mode()
