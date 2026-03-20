"""
SINC Orchestrator — Event Store
================================
Append-only event log for deterministic AI task execution.

Core principle
--------------
Events are the single source of truth.
State is a PROJECTION of events, not the origin.

The tasks table is a convenience store for operational queries.
The agent_events table + materialized views are authoritative.

Public API
----------
emit(event_type, task_id, actor, payload, tenant_id, project_id, correlation_id)
    → Append one event. The only write operation.

replay(task_id)
    → Generator[dict]: ordered event stream for a task.

project_state(task_id)
    → dict: current computed state from mv_task_projection.

llm_lineage(task_id)
    → list[dict]: all LLM calls for a task with prompts/responses.

refresh_projections()
    → Trigger REFRESH MATERIALIZED VIEW CONCURRENTLY on all views.

Standard event types (see 10_event_sourcing.sql for full taxonomy)
-------------------------------------------------------------------
Task lifecycle:
    task_created, task_queued, task_claimed, task_started,
    task_completed, task_failed, task_cancelled,
    task_dead_lettered, task_retry_scheduled, task_stale_recovered

AI decisions:
    llm_request_started, llm_request_finished, llm_request_failed

Code operations:
    patch_generated, patch_applied, patch_rejected,
    test_run_started, test_run_passed, test_run_failed

Review cycle:
    review_started, review_score_received, review_passed,
    review_failed, review_fix_requested
"""

from __future__ import annotations
from services.streaming.core.config import env_get

import json
import os
from typing import Any, Generator, Optional

# ── Database connection ───────────────────────────────────────────────────────

def _db_conn():
    """Return a database connection using the unified db context manager."""
    from services.streaming.core.db import db
    return db(bypass_rls=True)


# ── Core: emit ─────────────────────────────────────────────────────────────────

def emit(
    event_type: str,
    *,
    task_id: Optional[str]  = None,
    actor:   Optional[str]  = None,
    payload: Optional[dict] = None,
    tenant_id:      Optional[str] = None,
    project_id:     Optional[str] = None,
    correlation_id: Optional[str] = None,
) -> int:
    """
    Append one event to agent_events.

    Returns the new event id (BIGSERIAL).
    Raises RuntimeError on DB failure — callers should handle or log.

    The sequence_no within the task is set automatically by a DB trigger.

    Example
    -------
    emit("llm_request_finished",
         task_id="task-abc",
         actor="agent-worker-1",
         tenant_id="acme",
         project_id="sinc",
         payload={
             "model":         "claude-sonnet-4-6",
             "latency_ms":    1234,
             "input_tokens":  512,
             "output_tokens": 256,
             "prompt":        "Fix the auth bug in ...",
             "response":      "Here is the patch ...",
         })
    """
    if not event_type:
        raise ValueError("event_type is required")

    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO agent_events
                    (task_id, agent_name, event_type, payload,
                     tenant_id, project_id, correlation_id, actor)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    task_id,
                    actor,
                    event_type,
                    json.dumps(payload or {}),
                    tenant_id,
                    project_id,
                    correlation_id,
                    actor,
                ),
            )
            row = cur.fetchone()
            conn.commit()
            return row["id"] if isinstance(row, dict) else row[0]


# ── Core: replay ───────────────────────────────────────────────────────────────

def replay(task_id: str) -> Generator[dict, None, None]:
    """
    Yield all events for a task in chronological order.

    This is the deterministic replay API.
    Pass the events to a simulation function to reproduce any execution
    without calling external APIs.

    Example
    -------
    for event in replay("task-abc"):
        print(event["sequence_no"], event["event_type"], event["payload"])
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, task_id, sequence_no, event_type,
                       actor, agent_name, tenant_id, project_id,
                       correlation_id, payload, created_at
                FROM   agent_events
                WHERE  task_id = %s
                ORDER  BY sequence_no ASC NULLS LAST, id ASC
                """,
                (task_id,),
            )
            for row in cur.fetchall():
                yield dict(row) if not isinstance(row, dict) else row


# ── Core: project_state ────────────────────────────────────────────────────────

def project_state(task_id: str) -> dict:
    """
    Return current projected state for a task from mv_task_projection.

    Falls back to computing directly from agent_events if the
    materialized view hasn't been refreshed yet.

    Returns {} if no events exist for the task.

    Example
    -------
    state = project_state("task-abc")
    print(state["derived_status"])   # "done"
    print(state["llm_calls"])        # 3
    print(state["patches_applied"])  # 1
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            # Try materialized view first (fast)
            try:
                cur.execute(
                    "SELECT * FROM mv_task_projection WHERE task_id = %s",
                    (task_id,),
                )
                row = cur.fetchone()
                if row:
                    return dict(row) if not isinstance(row, dict) else row
            except Exception:
                pass  # view may not exist on older schema — fall through

            # Fallback: compute directly from raw events
            cur.execute(
                """
                SELECT
                    task_id,
                    COUNT(*)                                        AS total_events,
                    COUNT(*) FILTER (WHERE event_type LIKE 'llm_%%') AS llm_calls,
                    COUNT(*) FILTER (WHERE event_type = 'patch_applied') AS patches_applied,
                    COUNT(*) FILTER (WHERE event_type IN ('retry_triggered','retry_scheduled'))
                                                                    AS retry_count,
                    MIN(created_at)                                 AS first_event_at,
                    MAX(created_at)                                 AS last_event_at
                FROM   agent_events
                WHERE  task_id = %s
                GROUP  BY task_id
                """,
                (task_id,),
            )
            row = cur.fetchone()
            if not row:
                return {}
            return dict(row) if not isinstance(row, dict) else row


# ── LLM lineage ───────────────────────────────────────────────────────────────

def llm_lineage(task_id: str) -> list[dict]:
    """
    Return all LLM calls for a task with prompt/response/outcome.

    Uses mv_llm_lineage (fast) or falls back to raw agent_events.
    This is the dataset generation API — each row is a training sample.

    Example
    -------
    for call in llm_lineage("task-abc"):
        print(call["model"], call["latency_ms"], call["led_to_patch"])
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            try:
                cur.execute(
                    "SELECT * FROM mv_llm_lineage WHERE task_id = %s ORDER BY sequence_no",
                    (task_id,),
                )
                rows = cur.fetchall()
                return [dict(r) if not isinstance(r, dict) else r for r in rows]
            except Exception:
                pass

            # Fallback: raw events
            cur.execute(
                """
                SELECT id AS event_id, task_id, actor AS agent_name,
                       sequence_no, created_at, payload
                FROM   agent_events
                WHERE  task_id    = %s
                  AND  event_type = 'llm_request_finished'
                ORDER  BY sequence_no ASC NULLS LAST
                """,
                (task_id,),
            )
            rows = cur.fetchall()
            return [dict(r) if not isinstance(r, dict) else r for r in rows]


# ── Projection refresh ─────────────────────────────────────────────────────────

def refresh_projections() -> None:
    """
    Refresh all Event Sourcing materialized views concurrently.

    This is non-blocking (CONCURRENTLY) — reads continue during refresh.
    Called by the APScheduler in orchestrator_core.py every N minutes.
    """
    with _db_conn() as conn:
        with conn.cursor() as cur:
            # CONCURRENTLY requires a UNIQUE index on the view (set in migration)
            for view in ("mv_task_timeline", "mv_task_projection", "mv_llm_lineage"):
                try:
                    cur.execute(f"REFRESH MATERIALIZED VIEW CONCURRENTLY {view}")
                    conn.commit()
                except Exception as exc:
                    # View may not exist on older schema — log and continue
                    conn.rollback()
                    _warn(f"refresh_projections: could not refresh {view}: {exc}")


# ── Convenience emitters ───────────────────────────────────────────────────────
# These wrap emit() with structured payloads for the most common event types.

def emit_task_created(
    task_id: str, actor: str, title: str = "", description: str = "",
    urgency: str = "medium", tenant_id: str = "", project_id: str = "",
) -> int:
    return emit(
        "task_created", task_id=task_id, actor=actor,
        tenant_id=tenant_id, project_id=project_id,
        payload={"title": title, "description": description, "urgency": urgency},
    )


def emit_task_claimed(
    task_id: str, actor: str, claim_ttl_s: int = 120,
    tenant_id: str = "", project_id: str = "",
) -> int:
    return emit(
        "task_claimed", task_id=task_id, actor=actor,
        tenant_id=tenant_id, project_id=project_id,
        payload={"claim_ttl_s": claim_ttl_s},
    )


def emit_task_completed(
    task_id: str, actor: str, summary: str = "",
    files_modified: Optional[list] = None, backend: str = "",
    tenant_id: str = "", project_id: str = "",
) -> int:
    return emit(
        "task_completed", task_id=task_id, actor=actor,
        tenant_id=tenant_id, project_id=project_id,
        payload={
            "summary":        summary,
            "files_modified": files_modified or [],
            "backend":        backend,
        },
    )


def emit_task_failed(
    task_id: str, actor: str, reason: str = "",
    tenant_id: str = "", project_id: str = "",
) -> int:
    return emit(
        "task_failed", task_id=task_id, actor=actor,
        tenant_id=tenant_id, project_id=project_id,
        payload={"reason": reason},
    )


def emit_llm_call(
    task_id: str, actor: str, model: str,
    prompt: str, response: str,
    latency_ms: int = 0,
    input_tokens: int = 0, output_tokens: int = 0,
    tenant_id: str = "", project_id: str = "",
    correlation_id: Optional[str] = None,
) -> int:
    return emit(
        "llm_request_finished",
        task_id=task_id, actor=actor,
        tenant_id=tenant_id, project_id=project_id,
        correlation_id=correlation_id,
        payload={
            "model":         model,
            "latency_ms":    latency_ms,
            "input_tokens":  input_tokens,
            "output_tokens": output_tokens,
            "prompt":        prompt[:4096],    # cap to avoid row bloat
            "response":      response[:4096],
        },
    )


def emit_patch(
    event_type: str,     # "patch_generated" | "patch_applied" | "patch_rejected"
    task_id: str, actor: str,
    files: Optional[list] = None, reason: str = "",
    tenant_id: str = "", project_id: str = "",
) -> int:
    return emit(
        event_type, task_id=task_id, actor=actor,
        tenant_id=tenant_id, project_id=project_id,
        payload={"files": files or [], "reason": reason},
    )


# ── Internal ───────────────────────────────────────────────────────────────────

def _warn(msg: str) -> None:
    """Minimal stderr logger — avoids circular import with structlog."""
    import sys
    print(f"[event_store] WARNING: {msg}", file=sys.stderr)
