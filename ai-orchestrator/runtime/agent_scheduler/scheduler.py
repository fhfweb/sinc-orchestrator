"""
SINC Orchestrator Task Scheduler v2
Assigns pending tasks to agents using reputation-based scoring with
confidence intervals and exponential lock backoff.

Usage:
    python scheduler.py [--dry-run] [--max-assign N]
"""

import json
import math
import os
import sys
import time
import argparse
import asyncio
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Any


class DateTimeEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


def json_dumps(obj, **kwargs):
    return json.dumps(obj, cls=DateTimeEncoder, **kwargs)

import psycopg
import psycopg.rows

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────

BASE = Path(__file__).parent.parent.parent
REPUTATION_FILE = BASE / "agents" / "reputation.json"
WORKLOAD_FILE   = BASE / "agents" / "workload.json"
DISPATCHES_DIR  = BASE / "state" / "external-agent-bridge" / "dispatches"
PREFLIGHT_DIR   = BASE / "tasks" / "preflight"

DB_CONFIG = {
    "dbname":   os.environ.get("ORCH_DB_NAME",     "orchestrator_tasks"),
    "user":     os.environ.get("ORCH_DB_USER",     "orchestrator"),
    "password": os.environ.get("ORCH_DB_PASSWORD", ""),
    "host":     os.environ.get("ORCH_DB_HOST",     "localhost"),
    "port":     os.environ.get("ORCH_DB_PORT",     "5434"),
}

# Backoff config
BACKOFF_BASE_SECONDS   = 30     # first retry after 30s
BACKOFF_MAX_SECONDS    = 3600   # cap at 1 hour
BACKOFF_MULTIPLIER     = 2.0
MAX_AGENT_ACTIVE_TASKS = 2      # max concurrent tasks per agent
MIN_SAMPLES_VALID      = 100    # minimum samples for statistically valid score


# ─────────────────────────────────────────────
# WILSON SCORE CONFIDENCE INTERVAL (95%)
# https://en.wikipedia.org/wiki/Binomial_proportion_confidence_interval#Wilson_score_interval
# ─────────────────────────────────────────────

def wilson_interval(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 1.0
    p = successes / total
    denom = 1 + z**2 / total
    centre = (p + z**2 / (2 * total)) / denom
    spread = (z * math.sqrt(p * (1 - p) / total + z**2 / (4 * total**2))) / denom
    return max(0.0, centre - spread), min(1.0, centre + spread)


def confidence_level(samples: int) -> str:
    if samples >= MIN_SAMPLES_VALID:
        return "high"
    elif samples >= 50:
        return "medium"
    return "low"


from tenacity import retry, stop_after_attempt, wait_exponential

try:
    from services.event_bus import EventBus
    _HAS_EVENT_BUS = True
except ImportError:
    _HAS_EVENT_BUS = False

# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────

@retry(
    stop=stop_after_attempt(10),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    before_sleep=lambda retry_state: print(f"[scheduler] Retrying DB connection... (attempt {retry_state.attempt_number})")
)
def _db():
    return psycopg.connect(**DB_CONFIG, row_factory=psycopg.rows.dict_row)


def _now():
    return datetime.now(timezone.utc)


def _now_iso():
    return _now().isoformat()


# ─────────────────────────────────────────────
# REPUTATION LOADER
# ─────────────────────────────────────────────

def load_reputation() -> dict[str, dict]:
    """Load reputation from DB (preferred) or fall back to JSON file."""
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT * FROM agent_reputation")
                rows = cur.fetchall()
                if rows:
                    return {r["agent_name"]: dict(r) for r in rows}
    except Exception:
        pass

    # Fallback to JSON
    try:
        data = json.loads(REPUTATION_FILE.read_text(encoding="utf-8"))
        return {a["agent"]: a for a in data.get("agents", [])}
    except Exception:
        return {}


def agent_fit_score(agent: dict, task: dict, reputation: dict) -> float:
    """
    Compute assignment score for an agent given a task.
    Uses Wilson lower bound when samples < MIN_SAMPLES_VALID to be conservative.
    """
    name = agent.get("agent", agent.get("agent_name", ""))
    rep  = reputation.get(name, {})

    samples = rep.get("runtime_samples", 0)
    success_rate = rep.get("runtime_success_rate", 0.5)
    fit_score    = rep.get("reputation_fit_score", 0.5)

    # Use Wilson lower bound for agents with few samples
    if samples < MIN_SAMPLES_VALID:
        successes = int(success_rate * samples)
        lower, _ = wilson_interval(successes, max(samples, 1))
        effective_score = lower  # conservative estimate
        penalty = 1.0 - (samples / MIN_SAMPLES_VALID) * 0.3  # up to 30% penalty
    else:
        effective_score = fit_score
        penalty = 1.0

    # Skill affinity bonus (maps task metadata to skill dimensions)
    skill_bonus = _skill_affinity_bonus(rep, task)

    return effective_score * penalty + skill_bonus


def _skill_affinity_bonus(rep: dict, task: dict) -> float:
    """Add affinity bonus based on task domain keywords."""
    title = (task.get("title", "") + " " + task.get("description", "")).lower()
    bonus = 0.0
    if any(kw in title for kw in ["security", "auth", "rbac", "vuln"]):
        bonus += rep.get("qa", 0.1) * 0.1
    if any(kw in title for kw in ["deploy", "docker", "infra", "devops", "ci"]):
        bonus += rep.get("devops", 0.1) * 0.1
    if any(kw in title for kw in ["api", "controller", "service", "backend"]):
        bonus += rep.get("backend", 0.1) * 0.1
    if any(kw in title for kw in ["arch", "adr", "design", "pattern"]):
        bonus += rep.get("arch", 0.1) * 0.1
    return bonus


# ─────────────────────────────────────────────
# BACKOFF LOGIC
# ─────────────────────────────────────────────

def compute_backoff_until(conflict_count: int) -> datetime:
    """Exponential backoff: 30s, 60s, 120s, ... capped at 1h."""
    delay = min(BACKOFF_BASE_SECONDS * (BACKOFF_MULTIPLIER ** conflict_count),
                BACKOFF_MAX_SECONDS)
    return _now() + timedelta(seconds=delay)


def is_in_backoff(task: dict) -> bool:
    backoff_until = task.get("lock_backoff_until")
    if not backoff_until:
        return False
    if isinstance(backoff_until, str):
        try:
            backoff_until = datetime.fromisoformat(backoff_until)
        except ValueError:
            return False
    if backoff_until.tzinfo is None:
        backoff_until = backoff_until.replace(tzinfo=timezone.utc)
    return _now() < backoff_until


# ─────────────────────────────────────────────
# ACTIVE WORKLOAD
# ─────────────────────────────────────────────

def get_active_workload(conn) -> dict[str, int]:
    """Return count of in-progress tasks per agent."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT assigned_agent, COUNT(*) as cnt
            FROM tasks
            WHERE status = 'in-progress' AND assigned_agent IS NOT NULL
            GROUP BY assigned_agent
        """)
        return {r["assigned_agent"]: r["cnt"] for r in cur.fetchall()}


# ─────────────────────────────────────────────
# DEPENDENCY CHECK
# ─────────────────────────────────────────────

def dependencies_satisfied(task_id: str, conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN t.status = 'done' THEN 1 ELSE 0 END) as done_count
            FROM dependencies d
            JOIN tasks t ON t.id = d.dependency_id
            WHERE d.task_id = %s
        """, (task_id,))
        row = cur.fetchone()
        return row["total"] == 0 or row["total"] == row["done_count"]


# ─────────────────────────────────────────────
# DISPATCH ARTIFACT (filesystem + DB)
# ─────────────────────────────────────────────

def create_dispatch(task: dict, agent_name: str, dry_run: bool = False) -> bool:
    dispatch = {
        "task_id":         task["id"],
        "agent_name":      agent_name,
        "dispatched_at":   _now_iso(),
        "task":            task,
        "schema_version":  "v2-dispatch",
        "preflight_path":  str(PREFLIGHT_DIR / f"{task['id']}-{agent_name.replace(' ', '_')}.json"),
    }
    if dry_run:
        print(f"  [DRY-RUN] Would dispatch {task['id']} → {agent_name}")
        return True

    # Write filesystem artifact (backward compat)
    DISPATCHES_DIR.mkdir(parents=True, exist_ok=True)
    dispatch_file = DISPATCHES_DIR / f"{task['id']}.json"
    dispatch_file.write_text(json_dumps(dispatch, indent=2), encoding="utf-8")

    # Write to DB webhook_dispatches
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO webhook_dispatches (task_id, agent_name, dispatch_payload)
                    VALUES (%s, %s, %s)
                    ON CONFLICT DO NOTHING
                """, (task["id"], agent_name, json_dumps(dispatch)))
                cur.execute("""
                    INSERT INTO agent_events (task_id, agent_name, event_type, payload)
                    VALUES (%s, %s, 'dispatch', %s)
                """, (task["id"], agent_name, json_dumps({"dispatch": dispatch})))
                conn.commit()
    except Exception as e:
        print(f"  [WARN] DB dispatch insert failed: {e}")

    return True


# ─────────────────────────────────────────────
# MAIN SCHEDULER LOOP
# ─────────────────────────────────────────────

def run_scheduler(dry_run: bool = False, max_assign: int = 5):
    print(f"[scheduler] Starting (dry_run={dry_run}, max_assign={max_assign})")

    reputation = load_reputation()
    if not reputation:
        print("[scheduler] WARNING: No reputation data loaded")

    # Define available agents (could come from registry in future)
    available_agents = list(reputation.keys()) or [
        "AI Engineer", "AI Architect", "AI DevOps Engineer",
        "AI Security Engineer", "AI Product Manager",
        "Antigravity", "Claude", "Codex"
    ]

    assigned_count = 0

    try:
        with _db() as conn:
            workload = get_active_workload(conn)

            with conn.cursor() as cur:
                # Fetch all pending tasks ordered by priority
                cur.execute("""
                    SELECT * FROM tasks
                    WHERE status = 'pending'
                    ORDER BY
                        CASE priority
                            WHEN 'P0' THEN 0
                            WHEN 'P1' THEN 1
                            WHEN 'P2' THEN 2
                            ELSE 3
                        END,
                        CASE WHEN critical_path THEN 0 ELSE 1 END,
                        created_at ASC
                """)
                pending_tasks = cur.fetchall()

            print(f"[scheduler] {len(pending_tasks)} pending tasks, {sum(workload.values())} active")

            for task in pending_tasks:
                if assigned_count >= max_assign:
                    break

                task_id = task["id"]

                # Skip if in backoff window
                if is_in_backoff(task):
                    print(f"  [SKIP] {task_id} in backoff (conflicts={task.get('lock_conflict_count', 0)})")
                    continue

                # Skip if dependencies not met
                with _db() as check_conn:
                    if not dependencies_satisfied(task_id, check_conn):
                        print(f"  [SKIP] {task_id} has unsatisfied dependencies")
                        continue

                # Score all available agents for this task
                scored = []
                for agent_name in available_agents:
                    agent_load = workload.get(agent_name, 0)
                    if agent_load >= MAX_AGENT_ACTIVE_TASKS:
                        continue
                    score = agent_fit_score(
                        {"agent": agent_name}, task, reputation
                    )
                    scored.append((agent_name, score))

                if not scored:
                    print(f"  [SKIP] {task_id} — no available agents")
                    continue

                scored.sort(key=lambda x: x[1], reverse=True)
                best_agent, best_score = scored[0]

                print(f"  [ASSIGN] {task_id} → {best_agent} (score={best_score:.3f})")

                if not dry_run:
                    with _db() as upd_conn:
                        with upd_conn.cursor() as cur:
                            cur.execute("""
                                UPDATE tasks
                                SET status = 'in-progress',
                                    assigned_agent = %s,
                                    started_at = COALESCE(started_at, NOW()),
                                    updated_at = NOW()
                                WHERE id = %s AND status = 'pending'
                                RETURNING id
                            """, (best_agent, task_id))
                            if cur.fetchone():
                                cur.execute("""
                                    INSERT INTO agent_events (task_id, agent_name, event_type, payload)
                                    VALUES (%s, %s, 'start', %s)
                                """, (task_id, best_agent, json_dumps({
                                    "score": best_score,
                                    "confidence": confidence_level(
                                        reputation.get(best_agent, {}).get("runtime_samples", 0)
                                    )
                                })))
                                upd_conn.commit()

                create_dispatch(dict(task), best_agent, dry_run)
                workload[best_agent] = workload.get(best_agent, 0) + 1
                assigned_count += 1

    except Exception as e:
        print(f"[scheduler] ERROR: {e}")
        raise

    print(f"[scheduler] Done. Assigned {assigned_count} tasks.")
    return assigned_count


def handle_lock_conflict(task_id: str):
    """Called when a task encounters a lock conflict. Applies backoff."""
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE tasks
                    SET lock_conflict_count = COALESCE(lock_conflict_count, 0) + 1,
                        lock_retry_count    = COALESCE(lock_retry_count, 0) + 1,
                        lock_conflict_since = COALESCE(lock_conflict_since, NOW()),
                        lock_backoff_until  = %s,
                        status              = 'pending',
                        assigned_agent      = NULL,
                        updated_at          = NOW()
                    WHERE id = %s
                    RETURNING lock_conflict_count
                """, (compute_backoff_until(0).isoformat(), task_id))
                row = cur.fetchone()
                if row:
                    conflict_count = row["lock_conflict_count"]
                    backoff_until = compute_backoff_until(conflict_count)
                    cur.execute("""
                        UPDATE tasks SET lock_backoff_until = %s WHERE id = %s
                    """, (backoff_until.isoformat(), task_id))
                conn.commit()
        print(f"[scheduler] Lock conflict for {task_id} — backoff applied")
    except Exception as e:
        print(f"[scheduler] ERROR handling lock conflict: {e}")


# ─────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────

async def run_event_driven_scheduler(dry_run: bool, max_assign: int, interval: int):
    """
    Main entrypoint for Level 5 Event-Driven scheduling.
    Listens for Redis events but also maintains a safety poll.
    """
    print(f"[scheduler] Starting Event-Driven mode (Subscriber: orch:events, Safety Poll: {interval}s)")
    
    bus = await EventBus.get_instance()
    await bus.connect()

    # Shared state to prevent overlapping runs if multiple events arrive fast
    _busy = False

    async def trigger_scheduler(payload=None):
        nonlocal _busy
        if _busy: return
        _busy = True
        try:
            # We run the sync scheduler in a thread to keep the event loop free
            await asyncio.to_thread(run_scheduler, dry_run=dry_run, max_assign=max_assign)
        except Exception as e:
            print(f"[scheduler] Execution error: {e}")
        finally:
            _busy = False

    # 1. Start Background Safety Poll
    async def safety_poll():
        while True:
            await asyncio.sleep(interval)
            print("[scheduler] Safety poll triggered")
            await trigger_scheduler()

    asyncio.create_task(safety_poll())

    # 2. Main Subscription Loop
    print("[scheduler] Subscribing to orch:events...")
    await bus.subscribe("orch:events", trigger_scheduler)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC Task Scheduler v2")
    parser.add_argument("--dry-run",    action="store_true", help="Preview assignments without writing")
    parser.add_argument("--max-assign", type=int, default=5,  help="Max tasks to assign per run")
    parser.add_argument("--interval",   type=int, default=60, help="Safety poll interval in seconds")
    parser.add_argument("--lock-conflict", metavar="TASK_ID", help="Apply backoff for a lock conflict")
    args = parser.parse_args()

    if args.lock_conflict:
        handle_lock_conflict(args.lock_conflict)
    elif _HAS_EVENT_BUS:
        # Level 5: Event-Driven
        try:
            asyncio.run(run_event_driven_scheduler(args.dry_run, args.max_assign, args.interval))
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        # Legacy: Polling Loop
        print(f"[scheduler] Running in legacy loop mode — interval={args.interval}s")
        while True:
            try:
                run_scheduler(dry_run=args.dry_run, max_assign=args.max_assign)
            except Exception as exc:
                print(f"[scheduler] Loop error: {exc}")
            time.sleep(args.interval)
