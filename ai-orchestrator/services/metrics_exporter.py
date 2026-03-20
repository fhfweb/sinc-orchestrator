from services.streaming.core.config import env_get
"""
SINC Orchestrator Prometheus Metrics Exporter
Version: 2.0.0

Exposes orchestrator metrics in Prometheus text format on port 9090.

Endpoints:
  GET /metrics  — Prometheus text-format metrics
  GET /health   — JSON health summary

Data sources:
  - PostgreSQL (ORCH_DB_NAME, ORCH_DB_USER, ORCH_DB_PASSWORD, ORCH_DB_HOST, ORCH_DB_PORT)
  - state/health-report.json for health score

Runtime:
    FastAPI + uvicorn (canonical Python stack)
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import JSONResponse, PlainTextResponse
from services.streaming.core.config import BASE, DB_CONFIG, HEALTH_REPORT

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

HEALTH_REPORT_PATH = HEALTH_REPORT

METRICS_PORT = int(env_get("METRICS_PORT", default="9090"))

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("metrics_exporter")

# ─────────────────────────────────────────────────────────────────────────────
# ASGI APP
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(title="SINC Metrics Exporter", version="2.0.0")

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _db():
    """Open a new database connection using the unified db context manager."""
    from services.streaming.core.db import db
    return db(bypass_rls=True)


def _safe_db_query(sql: str, params: tuple = ()) -> list[dict]:
    """Execute a read query, returning rows as dicts. Returns [] on any error."""
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception as exc:
        log.warning("DB query failed: %s — %s", sql[:80], exc)
        return []


def _table_columns(table_name: str) -> set[str]:
    rows = _safe_db_query(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = %s
        """,
        (table_name,),
    )
    return {str(row["column_name"]) for row in rows}


def _task_pk() -> str:
    cols = _table_columns("tasks")
    return "task_id" if "task_id" in cols else "id"

# ─────────────────────────────────────────────────────────────────────────────
# METRIC COLLECTION
# ─────────────────────────────────────────────────────────────────────────────

KNOWN_STATUSES = [
    "done",
    "pending",
    "in-progress",
    "blocked-phase-approval",
    "blocked-lock-conflict",
]

KNOWN_PRIORITIES = ["P0", "P1", "P2"]


def _collect_task_counts_by_status() -> dict[str, int]:
    """orchestrator_tasks_total{status=...}"""
    rows = _safe_db_query(
        "SELECT status, COUNT(*) AS cnt FROM tasks GROUP BY status"
    )
    counts = {s: 0 for s in KNOWN_STATUSES}
    for row in rows:
        s = row["status"] or "unknown"
        counts[s] = counts.get(s, 0) + int(row["cnt"])
    return counts


def _collect_task_counts_by_priority() -> dict[str, int]:
    """orchestrator_tasks_priority{priority=...}"""
    rows = _safe_db_query(
        "SELECT priority, COUNT(*) AS cnt FROM tasks GROUP BY priority"
    )
    counts = {p: 0 for p in KNOWN_PRIORITIES}
    for row in rows:
        p = row["priority"] or "unknown"
        counts[p] = counts.get(p, 0) + int(row["cnt"])
    return counts


def _collect_active_agents() -> dict[str, int]:
    """orchestrator_agent_active{agent=...} — agents with in-progress tasks."""
    rows = _safe_db_query(
        """
        SELECT assigned_agent, COUNT(*) AS cnt
        FROM tasks
        WHERE status = 'in-progress' AND assigned_agent IS NOT NULL
        GROUP BY assigned_agent
        """
    )
    return {row["assigned_agent"]: int(row["cnt"]) for row in rows}


def _collect_lock_conflict_total() -> int:
    """orchestrator_lock_conflicts_total — sum of lock_conflict_count across all tasks."""
    rows = _safe_db_query(
        "SELECT COALESCE(SUM(COALESCE(lock_conflict_count, 0)), 0) AS total FROM tasks"
    )
    if rows:
        return int(rows[0]["total"])
    return 0


def _collect_heartbeat_orphans() -> int:
    """
    orchestrator_heartbeat_orphans_total — in-progress tasks missing a heartbeat
    for more than 90 seconds (mirrors heartbeat_monitor logic).
    """
    task_pk = _task_pk()
    rows = _safe_db_query(
        f"""
        SELECT COUNT(*) AS cnt
        FROM tasks t
        LEFT JOIN heartbeats hb
            ON hb.task_id = t.{task_pk} AND hb.agent_name = t.assigned_agent
        WHERE t.status = 'in-progress'
          AND t.assigned_agent IS NOT NULL
          AND (hb.beat_at IS NULL OR hb.beat_at < NOW() - INTERVAL '90 seconds')
          AND t.started_at < NOW() - INTERVAL '90 seconds'
        """
    )
    if rows:
        return int(rows[0]["cnt"])
    return 0


def _collect_repair_tasks_pending() -> int:
    """
    orchestrator_repair_tasks_pending — count of REPAIR-* tasks in
    pending or in-progress state.
    """
    task_pk = _task_pk()
    rows = _safe_db_query(
        f"""
        SELECT COUNT(*) AS cnt
        FROM tasks
        WHERE {task_pk} ILIKE 'REPAIR-%%'
          AND status IN ('pending', 'in-progress')
        """
    )
    if rows:
        return int(rows[0]["cnt"])
    return 0


def _collect_human_gates_pending() -> int:
    """orchestrator_human_gates_pending — tasks in blocked-phase-approval."""
    rows = _safe_db_query(
        """
        SELECT COUNT(*) AS cnt
        FROM tasks
        WHERE status = 'blocked-phase-approval'
        """
    )
    if rows:
        return int(rows[0]["cnt"])
    return 0


def _collect_health_score() -> float:
    """
    orchestrator_health_score (0–100).
    Uses DB-first runtime signals and falls back to state/health-report.json
    only as a compatibility projection.
    """
    # Primary: DB-first runtime heuristic
    try:
        rows = _safe_db_query(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'done')         AS done,
                COUNT(*) FILTER (WHERE status = 'failed')       AS failed,
                COUNT(*) FILTER (WHERE status = 'needs-revision') AS needs_revision,
                COUNT(*) FILTER (WHERE status = 'in-progress')  AS active,
                COUNT(*)                                         AS total
            FROM tasks
            """
        )
        if rows:
            r = rows[0]
            total  = int(r["total"]) or 1
            done   = int(r["done"])
            failed = int(r.get("failed") or 0)
            needs_revision = int(r.get("needs_revision") or 0)
            active = int(r["active"])
            orphans = _collect_heartbeat_orphans()
            policy_rows = _safe_db_query(
                """
                SELECT status, violations
                FROM policy_reports
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
            policy_status = (policy_rows[0]["status"] if policy_rows else "unknown")
            policy_violations = int(policy_rows[0]["violations"]) if policy_rows else 0

            # Runtime-weighted heuristic: completion, live work, and penalties for
            # stale/failed/policy-broken states.
            score = (done / total) * 60 + (active / total) * 20
            score -= min(orphans * 5, 30)
            score -= min((failed + needs_revision) * 8, 35)
            if policy_status not in ("ok", "unknown"):
                score -= 15
            score -= min(policy_violations * 2, 20)
            return round(max(0.0, min(100.0, score)), 1)
    except Exception as exc:
        log.debug("DB-first health score failed: %s", exc)

    # Compatibility fallback: projection file
    try:
        data = json.loads(HEALTH_REPORT_PATH.read_text(encoding="utf-8"))
        if "health_score" in data:
            return float(data["health_score"])
        checks = data.get("check_results", [])
        if checks:
            passed = sum(1 for c in checks if c.get("status") == "passed")
            score = (passed / len(checks)) * 100
            if data.get("health_status") == "degraded":
                score = min(score, 70.0)
            elif data.get("health_status") == "critical":
                score = min(score, 30.0)
            return round(score, 1)
    except Exception as exc:
        log.debug("Could not read health-report.json: %s", exc)

    try:
        rows = _safe_db_query(
            """
            SELECT
                COUNT(*) FILTER (WHERE status = 'done') AS done,
                COUNT(*) FILTER (WHERE status = 'in-progress') AS active,
                COUNT(*) AS total
            FROM tasks
            """
        )
        if rows:
            r = rows[0]
            total = int(r["total"]) or 1
            score = (int(r["done"]) / total) * 60 + (int(r["active"]) / total) * 20
            return round(max(0.0, min(100.0, score)), 1)
    except Exception:
        pass

    return 0.0


# ─────────────────────────────────────────────────────────────────────────────
# PROMETHEUS TEXT FORMAT RENDERER
# ─────────────────────────────────────────────────────────────────────────────

def _prom_gauge(name: str, help_text: str, labels_values: list[tuple[dict, float]]) -> str:
    """Render a Prometheus GAUGE metric block."""
    lines = [
        f"# HELP {name} {help_text}",
        f"# TYPE {name} gauge",
    ]
    for labels, value in labels_values:
        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in sorted(labels.items()))
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")
    return "\n".join(lines)


def _prom_counter(name: str, help_text: str, value: float) -> str:
    """Render a Prometheus COUNTER metric block (no labels)."""
    return (
        f"# HELP {name} {help_text}\n"
        f"# TYPE {name} counter\n"
        f"{name}_total {value}"
    )


def build_metrics() -> str:
    """Collect all metrics and render Prometheus text format."""
    blocks: list[str] = []
    collection_errors: list[str] = []

    # orchestrator_tasks_total{status=...}
    try:
        task_counts = _collect_task_counts_by_status()
        lv = [( {"status": s}, float(c) ) for s, c in task_counts.items()]
        blocks.append(_prom_gauge(
            "orchestrator_tasks_total",
            "Total tasks per status",
            lv,
        ))
    except Exception as e:
        collection_errors.append(f"task_counts: {e}")

    # orchestrator_tasks_priority{priority=...}
    try:
        priority_counts = _collect_task_counts_by_priority()
        lv = [( {"priority": p}, float(c) ) for p, c in priority_counts.items()]
        blocks.append(_prom_gauge(
            "orchestrator_tasks_priority",
            "Task count per priority level",
            lv,
        ))
    except Exception as e:
        collection_errors.append(f"priority_counts: {e}")

    # orchestrator_agent_active{agent=...}
    try:
        agent_active = _collect_active_agents()
        lv = [( {"agent": a}, float(c) ) for a, c in agent_active.items()]
        if not lv:
            lv = [( {"agent": "none"}, 0.0 )]
        blocks.append(_prom_gauge(
            "orchestrator_agent_active",
            "Number of in-progress tasks per agent",
            lv,
        ))
    except Exception as e:
        collection_errors.append(f"agent_active: {e}")

    # orchestrator_lock_conflicts_total (counter)
    try:
        conflict_total = _collect_lock_conflict_total()
        blocks.append(_prom_counter(
            "orchestrator_lock_conflicts",
            "Cumulative sum of lock_conflict_count across all tasks",
            float(conflict_total),
        ))
    except Exception as e:
        collection_errors.append(f"lock_conflicts: {e}")

    # orchestrator_heartbeat_orphans_total
    try:
        orphans = _collect_heartbeat_orphans()
        blocks.append(_prom_gauge(
            "orchestrator_heartbeat_orphans_total",
            "In-progress tasks with missing/stale heartbeats (>90s)",
            [( {}, float(orphans) )],
        ))
    except Exception as e:
        collection_errors.append(f"heartbeat_orphans: {e}")

    # orchestrator_repair_tasks_pending
    try:
        repair_pending = _collect_repair_tasks_pending()
        blocks.append(_prom_gauge(
            "orchestrator_repair_tasks_pending",
            "Count of REPAIR-* tasks in pending or in-progress state",
            [( {}, float(repair_pending) )],
        ))
    except Exception as e:
        collection_errors.append(f"repair_tasks_pending: {e}")

    # orchestrator_human_gates_pending
    try:
        gates_pending = _collect_human_gates_pending()
        blocks.append(_prom_gauge(
            "orchestrator_human_gates_pending",
            "Tasks blocked waiting for human phase-approval",
            [( {}, float(gates_pending) )],
        ))
    except Exception as e:
        collection_errors.append(f"human_gates_pending: {e}")

    # orchestrator_health_score
    try:
        health_score = _collect_health_score()
        blocks.append(_prom_gauge(
            "orchestrator_health_score",
            "Orchestrator overall health score (0-100)",
            [( {}, health_score )],
        ))
    except Exception as e:
        collection_errors.append(f"health_score: {e}")

    # Scrape metadata
    scrape_ts = int(time.time() * 1000)
    blocks.append(
        _prom_gauge(
            "orchestrator_last_scrape_timestamp_ms",
            "Unix timestamp (ms) of the last successful metrics scrape",
            [( {}, float(scrape_ts) )],
        )
    )

    if collection_errors:
        log.warning("Metric collection errors: %s", "; ".join(collection_errors))

    return "\n\n".join(blocks) + "\n"


# ─────────────────────────────────────────────────────────────────────────────
def generate_metrics() -> str:
    """Compatibility alias used by the canonical FastAPI control plane."""
    return build_metrics()


# ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/metrics")
def metrics():
    """Prometheus text-format metrics endpoint."""
    try:
        payload = build_metrics()
        return PlainTextResponse(payload, media_type="text/plain; version=0.0.4; charset=utf-8")
    except Exception as exc:
        log.error("Failed to build metrics: %s", exc)
        return PlainTextResponse(
            f"# ERROR building metrics: {exc}\n",
            status_code=500,
            media_type="text/plain",
        )


@app.get("/health")
def health():
    """JSON health summary endpoint."""
    summary: dict = {
        "status":       "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_reachable": False,
        "metrics":      {},
        "errors":       [],
    }

    # Test DB connectivity
    try:
        rows = _safe_db_query("SELECT 1 AS ping")
        summary["db_reachable"] = bool(rows)
    except Exception as exc:
        summary["errors"].append(f"db_check: {exc}")
        summary["status"] = "degraded"

    # Snapshot metrics
    try:
        summary["metrics"]["tasks_by_status"]   = _collect_task_counts_by_status()
        summary["metrics"]["tasks_by_priority"]  = _collect_task_counts_by_priority()
        summary["metrics"]["active_agents"]      = _collect_active_agents()
        summary["metrics"]["lock_conflicts"]     = _collect_lock_conflict_total()
        summary["metrics"]["heartbeat_orphans"]  = _collect_heartbeat_orphans()
        summary["metrics"]["repair_tasks_pending"]  = _collect_repair_tasks_pending()
        summary["metrics"]["human_gates_pending"]   = _collect_human_gates_pending()
        summary["metrics"]["health_score"]          = _collect_health_score()
    except Exception as exc:
        summary["errors"].append(f"metrics: {exc}")
        summary["status"] = "degraded"

    # Reflect health score in HTTP status
    health_score = summary["metrics"].get("health_score", 0)
    if health_score < 50:
        summary["status"] = "critical"
    elif health_score < 75:
        summary["status"] = "degraded"

    http_status = 200 if summary["status"] == "ok" else 503
    return JSONResponse(summary, status_code=http_status)


@app.get("/")
def index():
    return PlainTextResponse(
        "SINC Orchestrator Metrics Exporter\n"
        "  GET /metrics  - Prometheus text format\n"
        "  GET /health   - JSON health summary\n"
    )



# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting SINC Metrics Exporter on port %d", METRICS_PORT)
    log.info(
        "DB: %s@%s:%s/%s",
        DB_CONFIG["user"],
        DB_CONFIG["host"],
        DB_CONFIG["port"],
        DB_CONFIG["dbname"],
    )
    log.info(
        "DB password: %s",
        "set (*****)" if DB_CONFIG["password"] else "NOT SET — check ORCH_DB_PASSWORD",
    )
    log.info("Health report path: %s", HEALTH_REPORT_PATH)

    uvicorn.run(app, host="0.0.0.0", port=METRICS_PORT, log_level="info")
