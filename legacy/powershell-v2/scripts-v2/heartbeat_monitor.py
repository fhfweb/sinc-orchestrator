"""
SINC Orchestrator Heartbeat Monitor v2
Detects orphaned external agent tasks (no heartbeat for > 90s)
and resets them to 'pending' with backoff.

Usage:
    python heartbeat_monitor.py [--stale-threshold 90] [--dry-run]
"""

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import psycopg
import psycopg.rows

BASE = Path(__file__).parent.parent.parent
HEARTBEATS_DIR = BASE / "state" / "external-agent-bridge" / "heartbeats"
LOGS_DIR       = BASE / "logs"

DB_CONFIG = {
    "dbname":   os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":     os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", ""),
    "host":     os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":     os.environ.get("ORCH_DB_PORT",     "5434"),
}

DEFAULT_STALE_THRESHOLD_SECONDS = 90
BACKOFF_BASE_SECONDS = 60


from tenacity import retry, stop_after_attempt, wait_exponential

def _now():
    return datetime.now(timezone.utc)

def _log(msg: str):
    ts = _now().isoformat()
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "heartbeat_monitor.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

@retry(
    stop=stop_after_attempt(10),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    before_sleep=lambda retry_state: _log(f"Retrying DB connection... (attempt {retry_state.attempt_number})")
)
def _db():
    return psycopg.connect(**DB_CONFIG, row_factory=psycopg.rows.dict_row)


def check_filesystem_heartbeats(stale_threshold_seconds: int) -> set[str]:
    """
    Fallback: check heartbeat files written by agents that don't use the API.
    File pattern: state/external-agent-bridge/heartbeats/{task_id}.json
    Content: { "task_id": "...", "agent_name": "...", "beat_at": "ISO8601", "progress_pct": 50 }
    """
    stale_tasks = set()
    if not HEARTBEATS_DIR.exists():
        return stale_tasks

    cutoff = _now() - timedelta(seconds=stale_threshold_seconds)
    for hb_file in HEARTBEATS_DIR.glob("*.json"):
        try:
            data = json.loads(hb_file.read_text(encoding="utf-8"))
            beat_at = datetime.fromisoformat(data.get("beat_at", ""))
            if beat_at.tzinfo is None:
                beat_at = beat_at.replace(tzinfo=timezone.utc)
            if beat_at < cutoff:
                stale_tasks.add(data.get("task_id", hb_file.stem))
        except Exception:
            continue
    return stale_tasks


def run_monitor(stale_threshold_seconds: int = DEFAULT_STALE_THRESHOLD_SECONDS,
                dry_run: bool = False):
    _log(f"Starting heartbeat monitor (threshold={stale_threshold_seconds}s, dry_run={dry_run})")
    cutoff = _now() - timedelta(seconds=stale_threshold_seconds)
    orphaned_count = 0
    repaired_count = 0

    try:
        with _db() as conn:
            with conn.cursor() as cur:
                # Find in-progress tasks with no heartbeat since cutoff
                cur.execute("""
                    SELECT t.id, t.assigned_agent, t.started_at,
                           hb.beat_at, hb.progress_pct, hb.current_step
                    FROM tasks t
                    LEFT JOIN heartbeats hb
                        ON hb.task_id = t.id AND hb.agent_name = t.assigned_agent
                    WHERE t.status = 'in-progress'
                      AND t.assigned_agent IS NOT NULL
                      AND (hb.beat_at IS NULL OR hb.beat_at < %s)
                      AND t.started_at < %s
                """, (cutoff, cutoff))
                orphaned_tasks = cur.fetchall()

            # Also check filesystem heartbeats
            fs_stale = check_filesystem_heartbeats(stale_threshold_seconds)

            for task in orphaned_tasks:
                task_id = task["id"]
                agent   = task["assigned_agent"]
                last_hb = task["beat_at"]

                orphaned_count += 1
                idle_seconds = (_now() - (
                    last_hb.replace(tzinfo=timezone.utc) if last_hb else
                    task["started_at"].replace(tzinfo=timezone.utc)
                )).total_seconds()

                _log(f"  ORPHANED: {task_id} (agent={agent}, idle={idle_seconds:.0f}s, "
                     f"progress={task.get('progress_pct', '?')}%, step={task.get('current_step', '?')})")

                if not dry_run:
                    with _db() as repair_conn:
                        with repair_conn.cursor() as cur:
                            # Apply backoff
                            backoff_until = _now() + timedelta(seconds=BACKOFF_BASE_SECONDS)
                            cur.execute("""
                                UPDATE tasks
                                SET status = 'pending',
                                    assigned_agent = NULL,
                                    lock_backoff_until = %s,
                                    lock_conflict_count = COALESCE(lock_conflict_count, 0) + 1,
                                    updated_at = NOW()
                                WHERE id = %s AND status = 'in-progress'
                            """, (backoff_until.isoformat(), task_id))

                            # Log event
                            cur.execute("""
                                INSERT INTO agent_events (task_id, agent_name, event_type, payload)
                                VALUES (%s, %s, 'repair', %s)
                            """, (task_id, agent, json.dumps({
                                "reason": "heartbeat_timeout",
                                "idle_seconds": idle_seconds,
                                "backoff_until": backoff_until.isoformat()
                            })))

                            # Clear stale heartbeat
                            cur.execute(
                                "DELETE FROM heartbeats WHERE task_id = %s AND agent_name = %s",
                                (task_id, agent)
                            )
                            repair_conn.commit()

                    repaired_count += 1

            # Handle filesystem stale heartbeats not caught by DB
            for task_id in fs_stale:
                if task_id not in {t["id"] for t in orphaned_tasks}:
                    _log(f"  FS-STALE: {task_id} (heartbeat file is stale)")
                    orphaned_count += 1
                    if not dry_run:
                        try:
                            stale_file = HEARTBEATS_DIR / f"{task_id}.json"
                            if stale_file.exists():
                                stale_file.unlink()
                            with _db() as repair_conn:
                                with repair_conn.cursor() as cur:
                                    cur.execute("""
                                        UPDATE tasks
                                        SET status = 'pending', assigned_agent = NULL,
                                            lock_backoff_until = %s, updated_at = NOW()
                                        WHERE id = %s AND status = 'in-progress'
                                    """, ((_now() + timedelta(seconds=BACKOFF_BASE_SECONDS)).isoformat(), task_id))
                                    repair_conn.commit()
                            repaired_count += 1
                        except Exception as e:
                            _log(f"  ERROR repairing FS stale {task_id}: {e}")

    except Exception as e:
        _log(f"ERROR: {e}")
        sys.exit(1)

    _log(f"Done. Orphaned: {orphaned_count}, Repaired: {repaired_count}")
    return repaired_count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC Heartbeat Monitor v2")
    parser.add_argument("--stale-threshold", type=int, default=DEFAULT_STALE_THRESHOLD_SECONDS,
                        help=f"Seconds without heartbeat before marking task orphaned (default: {DEFAULT_STALE_THRESHOLD_SECONDS})")
    parser.add_argument("--dry-run", action="store_true", help="Detect orphans without repairing")
    parser.add_argument("--interval", type=int, default=0, help="Run in a loop every N seconds (0 = run once)")
    args = parser.parse_args()

    if args.interval > 0:
        _log(f"Running in loop mode — interval={args.interval}s")
        while True:
            try:
                run_monitor(stale_threshold_seconds=args.stale_threshold, dry_run=args.dry_run)
            except Exception as e:
                _log(f"Loop error: {e}")
            time.sleep(args.interval)
    else:
        run_monitor(stale_threshold_seconds=args.stale_threshold, dry_run=args.dry_run)
