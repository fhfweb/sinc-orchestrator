from services.streaming.core.config import env_get
"""
SINC Orchestrator P0 Webhook Alert Notifier
Version: 1.0.0

Polls the database every 60 seconds and fires webhook alerts for:
  - New P0 pending tasks
  - Tasks with lock_conflict_count > 50
  - Tasks orphaned (no heartbeat) for > 10 minutes

Webhook payload format:
    {
        "text":     "<human-readable summary>",
        "severity": "critical|warning",
        "task_id":  "<task id>",
        "details":  { ... }
    }

Alerts are also written to state/alerts.jsonl.
Deduplication: same (task_id, alert_type) pair is not re-sent within 30 minutes.
If ORCH_ALERT_WEBHOOK_URL is not set, the script continues and only writes locally.

Usage:
    python alert_notifier.py
    ORCH_ALERT_WEBHOOK_URL=https://hooks.example.com/xxx python alert_notifier.py
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
from services.http_client import create_sync_resilient_client
from services.streaming.core.config import BASE

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────

ALERTS_JSONL   = BASE / "state" / "alerts.jsonl"

WEBHOOK_URL      = env_get("ORCH_ALERT_WEBHOOK_URL", default="")

POLL_INTERVAL    = int(env_get("ALERT_POLL_INTERVAL_SECONDS", default="60"))
DEDUP_WINDOW     = int(env_get("ALERT_DEDUP_WINDOW_SECONDS", default="1800"))   # 30 min
LOCK_CONFLICT_THRESHOLD  = int(env_get("ALERT_LOCK_CONFLICT_THRESHOLD", default="50"))
ORPHAN_THRESHOLD_MINUTES = int(env_get("ALERT_ORPHAN_THRESHOLD_MINUTES", default="10"))
WEBHOOK_TIMEOUT  = int(env_get("ALERT_WEBHOOK_TIMEOUT_SECONDS", default="10"))

# ─────────────────────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("alert_notifier")

# ─────────────────────────────────────────────────────────────────────────────
# DB HELPER
# ─────────────────────────────────────────────────────────────────────────────

def _db():
    from services.streaming.core.db import db
    return db(bypass_rls=True)


def _safe_query(sql: str, params: tuple = ()) -> list[dict]:
    try:
        with _db() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                return cur.fetchall()
    except Exception as exc:
        log.warning("DB query failed: %s", exc)
        return []


_schema_cache: dict[str, set[str]] = {}


def _table_columns(table_name: str) -> set[str]:
    cached = _schema_cache.get(table_name)
    if cached is not None:
        return cached
    rows = _safe_query(
        """
        SELECT column_name AS name
          FROM information_schema.columns
         WHERE table_schema = 'public'
           AND table_name = %s
        """,
        (table_name,),
    )
    cols = {row["name"] for row in rows}
    _schema_cache[table_name] = cols
    return cols


def _task_pk_column() -> str:
    return "task_id" if "task_id" in _table_columns("tasks") else "id"


def _heartbeat_time_column() -> str:
    return "beat_at" if "beat_at" in _table_columns("heartbeats") else "updated_at"

# ─────────────────────────────────────────────────────────────────────────────
# DEDUPLICATION STATE (in-memory; reset on restart)
# ─────────────────────────────────────────────────────────────────────────────

# Key: (task_id, alert_type) → last sent timestamp
_dedup_cache: dict[tuple[str, str], datetime] = {}


def _is_deduped(task_id: str, alert_type: str) -> bool:
    """Return True if this alert was already sent within DEDUP_WINDOW seconds."""
    key = (task_id, alert_type)
    last = _dedup_cache.get(key)
    if last is None:
        return False
    return (datetime.now(timezone.utc) - last).total_seconds() < DEDUP_WINDOW


def _mark_sent(task_id: str, alert_type: str) -> None:
    _dedup_cache[(task_id, alert_type)] = datetime.now(timezone.utc)


def _evict_old_dedup_entries() -> None:
    """Periodically remove expired dedup entries to bound memory use."""
    now = datetime.now(timezone.utc)
    expired = [
        k for k, ts in _dedup_cache.items()
        if (now - ts).total_seconds() >= DEDUP_WINDOW
    ]
    for k in expired:
        del _dedup_cache[k]

# ─────────────────────────────────────────────────────────────────────────────
# ALERT PERSISTENCE (alerts.jsonl)
# ─────────────────────────────────────────────────────────────────────────────

def _write_alert_locally(payload: dict) -> None:
    """Append an alert payload to state/alerts.jsonl."""
    try:
        ALERTS_JSONL.parent.mkdir(parents=True, exist_ok=True)
        with open(ALERTS_JSONL, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("Could not write alert to %s: %s", ALERTS_JSONL, exc)

# ─────────────────────────────────────────────────────────────────────────────
# WEBHOOK DISPATCH
# ─────────────────────────────────────────────────────────────────────────────

def _send_webhook(payload: dict) -> bool:
    """
    POST payload as JSON to WEBHOOK_URL.
    Returns True on HTTP 2xx, False otherwise.
    Gracefully skips if WEBHOOK_URL is not configured.
    """
    if not WEBHOOK_URL:
        log.debug("No ORCH_ALERT_WEBHOOK_URL set — skipping webhook dispatch")
        return False

    try:
        with create_sync_resilient_client(
            service_name="alert-notifier",
            timeout=WEBHOOK_TIMEOUT,
            headers={"Content-Type": "application/json"},
        ) as client:
            resp = client.post(WEBHOOK_URL, json=payload)
            resp.raise_for_status()
        status_code = resp.status_code
        log.info("Webhook delivered: task=%s type=%s HTTP %d",
                 payload.get("task_id", "?"),
                 payload.get("alert_type", "?"),
                 status_code)
        return True
    except httpx.HTTPStatusError as exc:
        log.warning("Webhook HTTP error %d: %s", exc.response.status_code, exc)
        return False
    except Exception as exc:
        log.warning("Webhook delivery failed: %s", exc)
        return False


def _fire_alert(
    task_id: str,
    alert_type: str,
    text: str,
    severity: str,
    details: dict,
) -> None:
    """
    Fire an alert: write to alerts.jsonl + (optionally) send webhook.
    Deduplicates within DEDUP_WINDOW.
    """
    if _is_deduped(task_id, alert_type):
        log.debug("Suppressed duplicate alert: task=%s type=%s", task_id, alert_type)
        return

    payload = {
        "text":       text,
        "severity":   severity,
        "task_id":    task_id,
        "alert_type": alert_type,
        "fired_at":   datetime.now(timezone.utc).isoformat(),
        "details":    details,
    }

    log.info("[ALERT] %s | %s | %s", severity.upper(), alert_type, text)
    _write_alert_locally(payload)
    _send_webhook(payload)
    _mark_sent(task_id, alert_type)

# ─────────────────────────────────────────────────────────────────────────────
# DETECTION RULES
# ─────────────────────────────────────────────────────────────────────────────

def detect_p0_pending() -> list[dict]:
    """
    Rule: any task with priority='P0' and status='pending'.
    Rationale: P0 tasks should be picked up almost immediately; if still
    pending they may have been missed or blocked.
    """
    task_pk = _task_pk_column()
    rows = _safe_query(
        """
        SELECT {task_pk} AS id, title, status, priority, created_at, lock_conflict_count
        FROM tasks
        WHERE priority = 'P0' AND status = 'pending'
        ORDER BY created_at ASC
        """.format(task_pk=task_pk)
    )
    alerts = []
    for row in rows:
        task_id   = row["id"]
        created   = row["created_at"]
        age_mins  = 0.0
        if created:
            if hasattr(created, "tzinfo") and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age_mins = (datetime.now(timezone.utc) - created).total_seconds() / 60

        alerts.append({
            "task_id":    task_id,
            "alert_type": "p0_pending",
            "text": (
                f"P0 task {task_id} is PENDING (unassigned) "
                f"for {age_mins:.1f} min: {row.get('title', '')}"
            ),
            "severity": "critical",
            "details": {
                "task_id":    task_id,
                "title":      row.get("title", ""),
                "created_at": str(created),
                "age_minutes": round(age_mins, 1),
                "lock_conflict_count": row.get("lock_conflict_count", 0),
            },
        })
    return alerts


def detect_high_lock_conflicts() -> list[dict]:
    """
    Rule: tasks with lock_conflict_count > LOCK_CONFLICT_THRESHOLD.
    Rationale: many conflicts indicate a scheduling/locking pathology.
    """
    task_pk = _task_pk_column()
    rows = _safe_query(
        """
        SELECT {task_pk} AS id, title, status, priority, lock_conflict_count,
               assigned_agent, updated_at
        FROM tasks
        WHERE COALESCE(lock_conflict_count, 0) > %s
        ORDER BY lock_conflict_count DESC
        """.format(task_pk=task_pk),
        (LOCK_CONFLICT_THRESHOLD,),
    )
    alerts = []
    for row in rows:
        task_id      = row["id"]
        conflict_cnt = int(row["lock_conflict_count"] or 0)
        alerts.append({
            "task_id":    task_id,
            "alert_type": "high_lock_conflicts",
            "text": (
                f"Task {task_id} has {conflict_cnt} lock conflicts "
                f"(threshold={LOCK_CONFLICT_THRESHOLD}): {row.get('title', '')}"
            ),
            "severity": "critical" if conflict_cnt > LOCK_CONFLICT_THRESHOLD * 2 else "warning",
            "details": {
                "task_id":            task_id,
                "title":              row.get("title", ""),
                "status":             row.get("status", ""),
                "priority":           row.get("priority", ""),
                "lock_conflict_count": conflict_cnt,
                "assigned_agent":     row.get("assigned_agent", ""),
                "updated_at":         str(row.get("updated_at", "")),
                "threshold":          LOCK_CONFLICT_THRESHOLD,
            },
        })
    return alerts


def detect_orphaned_tasks() -> list[dict]:
    """
    Rule: in-progress tasks with no heartbeat for > ORPHAN_THRESHOLD_MINUTES.
    Rationale: agent may have crashed or lost connection.
    """
    task_pk = _task_pk_column()
    heartbeat_time_col = _heartbeat_time_column()
    threshold_secs = ORPHAN_THRESHOLD_MINUTES * 60
    rows = _safe_query(
        """
        SELECT t.{task_pk} AS id, t.title, t.assigned_agent, t.started_at,
               hb.{heartbeat_time_col} AS beat_at, t.priority
        FROM tasks t
        LEFT JOIN heartbeats hb
            ON hb.task_id = t.{task_pk} AND hb.agent_name = t.assigned_agent
        WHERE t.status = 'in-progress'
          AND t.assigned_agent IS NOT NULL
          AND (hb.{heartbeat_time_col} IS NULL OR hb.{heartbeat_time_col} < NOW() - %s * INTERVAL '1 second')
          AND t.started_at < NOW() - %s * INTERVAL '1 second'
        ORDER BY t.started_at ASC
        """.format(task_pk=task_pk, heartbeat_time_col=heartbeat_time_col),
        (threshold_secs, threshold_secs),
    )
    alerts = []
    for row in rows:
        task_id   = row["id"]
        beat_at   = row.get("beat_at")
        started   = row.get("started_at")

        # Calculate idle time
        reference = beat_at or started
        idle_mins = 0.0
        if reference:
            if hasattr(reference, "tzinfo") and reference.tzinfo is None:
                reference = reference.replace(tzinfo=timezone.utc)
            idle_mins = (datetime.now(timezone.utc) - reference).total_seconds() / 60

        alerts.append({
            "task_id":    task_id,
            "alert_type": "orphaned_task",
            "text": (
                f"Task {task_id} orphaned: agent={row.get('assigned_agent', '?')} "
                f"idle={idle_mins:.1f} min (threshold={ORPHAN_THRESHOLD_MINUTES} min)"
            ),
            "severity": "critical" if (row.get("priority") == "P0") else "warning",
            "details": {
                "task_id":        task_id,
                "title":          row.get("title", ""),
                "priority":       row.get("priority", ""),
                "assigned_agent": row.get("assigned_agent", ""),
                "last_heartbeat": str(beat_at) if beat_at else None,
                "started_at":     str(started) if started else None,
                "idle_minutes":   round(idle_mins, 1),
                "threshold_minutes": ORPHAN_THRESHOLD_MINUTES,
            },
        })
    return alerts

# ─────────────────────────────────────────────────────────────────────────────
# POLL LOOP
# ─────────────────────────────────────────────────────────────────────────────

def run_poll_loop() -> None:
    log.info("SINC Alert Notifier starting")
    log.info("Poll interval  : %ds", POLL_INTERVAL)
    log.info("Dedup window   : %ds (%.0f min)", DEDUP_WINDOW, DEDUP_WINDOW / 60)
    log.info("Lock threshold : %d conflicts", LOCK_CONFLICT_THRESHOLD)
    log.info("Orphan timeout : %d min", ORPHAN_THRESHOLD_MINUTES)
    log.info(
        "Webhook URL    : %s",
        WEBHOOK_URL if WEBHOOK_URL else "NOT SET — local-only mode",
    )
    log.info(
        "DB password    : %s",
        "set (*****)" if DB_CONFIG["password"] else "NOT SET — check ORCH_DB_PASSWORD",
    )
    log.info("Alerts JSONL   : %s", ALERTS_JSONL)

    iteration = 0
    while True:
        iteration += 1
        log.debug("Poll iteration #%d", iteration)

        # Run all detection rules; each is isolated so one failing rule
        # does not prevent the others from running.
        all_pending_alerts: list[dict] = []

        try:
            all_pending_alerts.extend(detect_p0_pending())
        except Exception as exc:
            log.error("detect_p0_pending failed: %s", exc)

        try:
            all_pending_alerts.extend(detect_high_lock_conflicts())
        except Exception as exc:
            log.error("detect_high_lock_conflicts failed: %s", exc)

        try:
            all_pending_alerts.extend(detect_orphaned_tasks())
        except Exception as exc:
            log.error("detect_orphaned_tasks failed: %s", exc)

        for alert in all_pending_alerts:
            _fire_alert(
                task_id    = alert["task_id"],
                alert_type = alert["alert_type"],
                text       = alert["text"],
                severity   = alert["severity"],
                details    = alert["details"],
            )

        if all_pending_alerts:
            log.info(
                "Poll #%d: %d alert(s) evaluated (%d unique keys in dedup cache)",
                iteration,
                len(all_pending_alerts),
                len(_dedup_cache),
            )
        else:
            log.debug("Poll #%d: no alerts", iteration)

        # Periodically evict stale dedup entries
        if iteration % 10 == 0:
            _evict_old_dedup_entries()

        time.sleep(POLL_INTERVAL)


# ─────────────────────────────────────────────────────────────────────────────
# ENTRYPOINT
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    try:
        run_poll_loop()
    except KeyboardInterrupt:
        log.info("Alert notifier stopped by user (KeyboardInterrupt)")
        sys.exit(0)
    except Exception as exc:
        log.critical("Fatal error in alert notifier: %s", exc, exc_info=True)
        sys.exit(1)
