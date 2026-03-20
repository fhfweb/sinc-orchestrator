"""
SINC Orchestrator Whiteboard Enforcer v2
Ensures every agent announces intent on the whiteboard BEFORE acquiring a task lock.
Detects tasks that are in-progress without a corresponding whiteboard announcement
and either blocks them or creates retroactive announcements.

Usage:
    python whiteboard_enforcer.py [--mode check|enforce|retroactive]
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

# Grace period: allow tasks that just started (< 30s) without announcement
GRACE_PERIOD_SECONDS = 30


def _db():
    return psycopg.connect(**DB_CONFIG, row_factory=psycopg.rows.dict_row)


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


def _log(msg: str):
    ts = _now_iso()
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "whiteboard_enforcer.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _read_whiteboard() -> dict:
    try:
        return json.loads(WHITEBOARD.read_text(encoding="utf-8"))
    except Exception:
        return {"entries": [], "updated_at": _now_iso()}


def _write_whiteboard(wb: dict):
    wb["updated_at"] = _now_iso()
    WHITEBOARD.write_text(json.dumps(wb, indent=4, ensure_ascii=False), encoding="utf-8")


def get_announced_task_ids(wb: dict) -> set[str]:
    return {
        e["task_id"] for e in wb.get("entries", [])
        if e.get("status") in {"announced", "completed"}
    }


def check_mode() -> list[dict]:
    """Check which in-progress tasks lack whiteboard announcements. Report only."""
    wb = _read_whiteboard()
    announced = get_announced_task_ids(wb)
    violations = []

    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cutoff = _now() - timedelta(seconds=GRACE_PERIOD_SECONDS)
                cur.execute("""
                    SELECT id, assigned_agent, started_at, updated_at
                    FROM tasks
                    WHERE status = 'in-progress'
                      AND assigned_agent IS NOT NULL
                      AND started_at < %s
                """, (cutoff,))
                in_progress = cur.fetchall()
    except Exception as e:
        _log(f"ERROR fetching tasks: {e}")
        return []

    for task in in_progress:
        if task["id"] not in announced:
            violations.append({
                "task_id":      task["id"],
                "agent":        task["assigned_agent"],
                "started_at":   task["started_at"].isoformat() if task["started_at"] else None,
                "violation":    "no-whiteboard-announcement",
            })

    if violations:
        _log(f"VIOLATIONS ({len(violations)} tasks running without whiteboard announcement):")
        for v in violations:
            _log(f"  {v['task_id']} (agent={v['agent']})")
    else:
        _log("OK: All in-progress tasks have whiteboard announcements")

    return violations


def enforce_mode() -> int:
    """
    Enforce: suspend in-progress tasks that lack whiteboard announcements
    (reset to pending so scheduler re-dispatches with proper announcement).
    """
    violations = check_mode()
    if not violations:
        return 0

    suspended = 0
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                for v in violations:
                    task_id = v["task_id"]
                    _log(f"  SUSPEND {task_id} (missing whiteboard) → pending")
                    cur.execute("""
                        UPDATE tasks
                        SET status = 'pending', assigned_agent = NULL, updated_at = NOW()
                        WHERE id = %s AND status = 'in-progress'
                    """, (task_id,))
                    if cur.rowcount:
                        cur.execute("""
                            INSERT INTO agent_events (task_id, agent_name, event_type, payload)
                            VALUES (%s, %s, 'repair', %s)
                        """, (task_id, v["agent"], json.dumps({
                            "reason": "whiteboard_enforcement",
                            "action": "suspended_to_pending"
                        })))
                        suspended += 1
                conn.commit()
    except Exception as e:
        _log(f"ERROR enforcing: {e}")
        return 0

    _log(f"Suspended {suspended} tasks for missing whiteboard announcements")
    return suspended


def retroactive_mode() -> int:
    """
    Retroactive: create whiteboard announcements for all in-progress tasks
    that don't have one (catch-up for existing tasks before enforcement is active).
    """
    violations = check_mode()
    if not violations:
        return 0

    wb = _read_whiteboard()
    added = 0

    for v in violations:
        task_id = v["task_id"]
        agent   = v["agent"]

        # Avoid duplicates
        existing = [e for e in wb["entries"] if e["task_id"] == task_id]
        if existing:
            continue

        _log(f"  RETROACTIVE announcement: {task_id} (agent={agent})")
        wb["entries"].append({
            "task_id":       task_id,
            "agent":         agent,
            "intention":     f"Retroactive announcement for {task_id}",
            "files_intended": [],
            "status":        "announced",
            "announced_at":  v.get("started_at") or _now_iso(),
            "completed_at":  "",
            "handoff_to":    "",
            "retroactive":   True,
        })
        added += 1

    if added:
        _write_whiteboard(wb)
        _log(f"Added {added} retroactive announcements")

    return added


def purge_stale_announcements(max_age_hours: int = 48) -> int:
    """Remove completed or very old announcements from the whiteboard."""
    wb = _read_whiteboard()
    cutoff = _now() - timedelta(hours=max_age_hours)
    original_count = len(wb["entries"])

    def is_stale(entry: dict) -> bool:
        if entry.get("status") == "completed":
            completed = entry.get("completed_at", "")
            if completed:
                try:
                    ts = datetime.fromisoformat(completed)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    return ts < cutoff
                except ValueError:
                    return True
        announced = entry.get("announced_at", "")
        if announced:
            try:
                ts = datetime.fromisoformat(announced)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                return ts < cutoff and entry.get("status") != "announced"
            except ValueError:
                pass
        return False

    wb["entries"] = [e for e in wb["entries"] if not is_stale(e)]
    purged = original_count - len(wb["entries"])

    if purged:
        _write_whiteboard(wb)
        _log(f"Purged {purged} stale whiteboard entries")

    return purged


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC Whiteboard Enforcer v2")
    parser.add_argument(
        "--mode",
        choices=["check", "enforce", "retroactive", "purge"],
        default="check",
        help="check=report violations, enforce=suspend violators, "
             "retroactive=add missing announcements, purge=clean stale entries"
    )
    parser.add_argument("--max-age-hours", type=int, default=48,
                        help="Hours before completed entries are purged (default: 48)")
    args = parser.parse_args()

    if args.mode == "check":
        violations = check_mode()
        sys.exit(1 if violations else 0)
    elif args.mode == "enforce":
        count = enforce_mode()
        sys.exit(0)
    elif args.mode == "retroactive":
        count = retroactive_mode()
        sys.exit(0)
    elif args.mode == "purge":
        count = purge_stale_announcements(max_age_hours=args.max_age_hours)
        sys.exit(0)
