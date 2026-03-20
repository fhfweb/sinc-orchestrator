-- SINC Orchestrator Schema — Migration 10
-- Event Sourcing "Lite": Immutable Event Log + Projections
--
-- Philosophy:
--   agent_events is the SOURCE OF TRUTH.
--   All state (task status, agent decisions, LLM calls) is derived from events.
--   The tasks table remains, but its status is a PROJECTION of events — not the origin.
--
-- What this migration adds:
--   1. New columns on agent_events (tenant_id, project_id, correlation_id, sequence_no, actor)
--   2. Append-only enforcement trigger (no UPDATE / DELETE ever)
--   3. mv_task_timeline  — ordered event stream per task (for debug/replay)
--   4. mv_task_projection — latest computed state per task (for dashboard reads)
--   5. refresh_projections() — called by the scheduler to refresh views
-- ─────────────────────────────────────────────────────────────────────────────


-- ─────────────────────────────────────────────
-- 1. EXTEND agent_events (safe, idempotent)
-- ─────────────────────────────────────────────

ALTER TABLE agent_events
    ADD COLUMN IF NOT EXISTS tenant_id      TEXT,
    ADD COLUMN IF NOT EXISTS project_id     TEXT,
    ADD COLUMN IF NOT EXISTS correlation_id TEXT,       -- group related events (e.g. one task execution cycle)
    ADD COLUMN IF NOT EXISTS sequence_no    BIGINT,     -- monotonic within a task
    ADD COLUMN IF NOT EXISTS actor          TEXT;       -- explicit actor (agent name, service, 'scheduler', etc.)

-- Populate actor from existing agent_name where actor is null
UPDATE agent_events SET actor = agent_name WHERE actor IS NULL AND agent_name IS NOT NULL;

-- Backfill sequence_no for existing rows (per task, ordered by id)
WITH numbered AS (
    SELECT id,
           ROW_NUMBER() OVER (PARTITION BY task_id ORDER BY id) AS seq
    FROM agent_events
    WHERE sequence_no IS NULL AND task_id IS NOT NULL
)
UPDATE agent_events ae
SET sequence_no = n.seq
FROM numbered n
WHERE ae.id = n.id;

-- Additional indexes for event sourcing query patterns
CREATE INDEX IF NOT EXISTS idx_ae_tenant_project
    ON agent_events(tenant_id, project_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ae_correlation
    ON agent_events(correlation_id) WHERE correlation_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ae_task_seq
    ON agent_events(task_id, sequence_no);


-- ─────────────────────────────────────────────
-- 2. APPEND-ONLY ENFORCEMENT TRIGGER
--
-- Events must NEVER be modified or deleted.
-- Any attempt raises an exception that propagates to the application.
-- ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION _prevent_agent_events_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
BEGIN
    RAISE EXCEPTION
        'agent_events is append-only — UPDATE and DELETE are forbidden. '
        'Event id=% type=% task_id=%',
        OLD.id, OLD.event_type, OLD.task_id;
END;
$$;

DROP TRIGGER IF EXISTS trg_ae_immutable ON agent_events;

CREATE TRIGGER trg_ae_immutable
BEFORE UPDATE OR DELETE ON agent_events
FOR EACH ROW EXECUTE FUNCTION _prevent_agent_events_mutation();


-- ─────────────────────────────────────────────
-- 3. AUTO-INCREMENT sequence_no ON INSERT
-- ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION _ae_set_sequence_no()
RETURNS TRIGGER
LANGUAGE plpgsql AS
$$
DECLARE
    next_seq BIGINT;
BEGIN
    IF NEW.task_id IS NOT NULL AND NEW.sequence_no IS NULL THEN
        SELECT COALESCE(MAX(sequence_no), 0) + 1
        INTO   next_seq
        FROM   agent_events
        WHERE  task_id = NEW.task_id;

        NEW.sequence_no := next_seq;
    END IF;

    -- Populate actor from agent_name if not set
    IF NEW.actor IS NULL AND NEW.agent_name IS NOT NULL THEN
        NEW.actor := NEW.agent_name;
    END IF;

    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_ae_sequence ON agent_events;

CREATE TRIGGER trg_ae_sequence
BEFORE INSERT ON agent_events
FOR EACH ROW EXECUTE FUNCTION _ae_set_sequence_no();


-- ─────────────────────────────────────────────
-- 4. MATERIALIZED VIEW: mv_task_timeline
--
-- Full ordered event stream per task.
-- Used for: forensic debug, replay, dataset generation.
-- Refresh: REFRESH MATERIALIZED VIEW CONCURRENTLY mv_task_timeline;
-- ─────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_task_timeline AS
SELECT
    ae.task_id,
    ae.sequence_no,
    ae.event_type,
    ae.actor,
    ae.agent_name,
    ae.tenant_id,
    ae.project_id,
    ae.correlation_id,
    ae.payload,
    ae.created_at,
    -- Human-readable delta from previous event in same task
    EXTRACT(EPOCH FROM (
        ae.created_at - LAG(ae.created_at) OVER (
            PARTITION BY ae.task_id ORDER BY ae.sequence_no
        )
    ))::NUMERIC(10,3)                                         AS delta_seconds
FROM   agent_events ae
WHERE  ae.task_id IS NOT NULL
ORDER  BY ae.task_id, ae.sequence_no;

CREATE UNIQUE INDEX IF NOT EXISTS uidx_mv_task_timeline
    ON mv_task_timeline(task_id, sequence_no);

CREATE INDEX IF NOT EXISTS idx_mv_timeline_tenant
    ON mv_task_timeline(tenant_id, created_at DESC);


-- ─────────────────────────────────────────────
-- 5. MATERIALIZED VIEW: mv_task_projection
--
-- Current computed state per task, derived entirely from events.
-- This is the "read model" — the dashboard and APIs read this.
-- It answers: "what is the current state of task X?" without
-- relying on mutable columns in the tasks table.
-- ─────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_task_projection AS
WITH
event_counts AS (
    SELECT
        task_id,
        COUNT(*)                                                              AS total_events,
        COUNT(*) FILTER (WHERE event_type LIKE 'llm_%')                       AS llm_calls,
        COUNT(*) FILTER (WHERE event_type = 'llm_request_finished')           AS llm_successes,
        COUNT(*) FILTER (WHERE event_type = 'llm_request_failed')             AS llm_failures,
        COUNT(*) FILTER (WHERE event_type = 'patch_generated')                AS patches_generated,
        COUNT(*) FILTER (WHERE event_type = 'patch_applied')                  AS patches_applied,
        COUNT(*) FILTER (WHERE event_type = 'patch_rejected')                 AS patches_rejected,
        COUNT(*) FILTER (WHERE event_type = 'test_run_passed')                AS tests_passed,
        COUNT(*) FILTER (WHERE event_type = 'test_run_failed')                AS tests_failed,
        COUNT(*) FILTER (WHERE event_type IN ('retry_triggered','retry_scheduled')) AS retry_count,
        COUNT(*) FILTER (WHERE event_type = 'review_score_received')          AS review_cycles,
        MIN(created_at)                                                        AS first_event_at,
        MAX(created_at)                                                        AS last_event_at,
        MIN(tenant_id)                                                         AS tenant_id,
        MIN(project_id)                                                        AS project_id
    FROM   agent_events
    WHERE  task_id IS NOT NULL
    GROUP  BY task_id
),
last_events AS (
    -- Most recent event per task (determines derived status)
    SELECT DISTINCT ON (task_id)
        task_id,
        event_type   AS last_event_type,
        actor        AS last_actor,
        payload      AS last_payload
    FROM   agent_events
    WHERE  task_id IS NOT NULL
    ORDER  BY task_id, id DESC
),
terminal_events AS (
    -- Detect terminal state from events
    SELECT DISTINCT ON (task_id)
        task_id,
        CASE
            WHEN event_type = 'task_completed'     THEN 'done'
            WHEN event_type = 'task_failed'        THEN 'failed'
            WHEN event_type = 'task_cancelled'     THEN 'cancelled'
            WHEN event_type = 'task_dead_lettered' THEN 'dead-letter'
            WHEN event_type = 'task_started'       THEN 'in-progress'
            WHEN event_type = 'task_claimed'       THEN 'in-progress'
            WHEN event_type = 'task_queued'        THEN 'pending'
            WHEN event_type = 'task_created'       THEN 'pending'
            WHEN event_type IN ('retry_triggered','retry_scheduled') THEN 'pending'
            ELSE NULL
        END AS derived_status
    FROM   agent_events
    WHERE  task_id IS NOT NULL
      AND  event_type IN (
               'task_completed','task_failed','task_cancelled','task_dead_lettered',
               'task_started','task_claimed','task_queued','task_created',
               'retry_triggered','retry_scheduled'
           )
    ORDER  BY task_id, id DESC
),
-- Compute avg LLM latency from payload
llm_latency AS (
    SELECT
        task_id,
        AVG((payload->>'latency_ms')::NUMERIC) AS avg_llm_latency_ms
    FROM   agent_events
    WHERE  event_type = 'llm_request_finished'
      AND  payload->>'latency_ms' IS NOT NULL
    GROUP  BY task_id
)
SELECT
    ec.task_id,
    ec.tenant_id,
    ec.project_id,
    COALESCE(te.derived_status, 'unknown')  AS derived_status,
    le.last_event_type,
    le.last_actor,
    ec.total_events,
    ec.llm_calls,
    ec.llm_successes,
    ec.llm_failures,
    ec.patches_generated,
    ec.patches_applied,
    ec.patches_rejected,
    ec.tests_passed,
    ec.tests_failed,
    ec.retry_count,
    ec.review_cycles,
    ll.avg_llm_latency_ms,
    ec.first_event_at,
    ec.last_event_at,
    EXTRACT(EPOCH FROM (ec.last_event_at - ec.first_event_at))::NUMERIC(10,1)
                                            AS total_duration_seconds
FROM       event_counts    ec
LEFT JOIN  last_events     le ON le.task_id = ec.task_id
LEFT JOIN  terminal_events te ON te.task_id = ec.task_id
LEFT JOIN  llm_latency     ll ON ll.task_id = ec.task_id;

CREATE UNIQUE INDEX IF NOT EXISTS uidx_mv_task_projection
    ON mv_task_projection(task_id);

CREATE INDEX IF NOT EXISTS idx_mv_projection_tenant
    ON mv_task_projection(tenant_id, project_id);

CREATE INDEX IF NOT EXISTS idx_mv_projection_status
    ON mv_task_projection(derived_status);


-- ─────────────────────────────────────────────
-- 6. MATERIALIZED VIEW: mv_llm_lineage
--
-- Links every LLM call to its task, prompt, response and outcome.
-- Used for: dataset generation, fine-tuning, auditing AI decisions.
-- ─────────────────────────────────────────────

CREATE MATERIALIZED VIEW IF NOT EXISTS mv_llm_lineage AS
SELECT
    ae.id              AS event_id,
    ae.task_id,
    ae.tenant_id,
    ae.project_id,
    ae.correlation_id,
    ae.actor           AS agent_name,
    ae.sequence_no,
    ae.created_at,
    ae.payload->>'model'        AS model,
    ae.payload->>'prompt'       AS prompt,
    ae.payload->>'response'     AS response,
    ae.payload->>'latency_ms'   AS latency_ms,
    ae.payload->>'input_tokens' AS input_tokens,
    ae.payload->>'output_tokens' AS output_tokens,
    -- Outcome: did a patch get applied after this LLM call?
    EXISTS (
        SELECT 1 FROM agent_events ae2
        WHERE  ae2.task_id     = ae.task_id
          AND  ae2.sequence_no > ae.sequence_no
          AND  ae2.event_type  = 'patch_applied'
    ) AS led_to_patch
FROM   agent_events ae
WHERE  ae.event_type = 'llm_request_finished'
  AND  ae.task_id   IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS uidx_mv_llm_lineage
    ON mv_llm_lineage(event_id);

CREATE INDEX IF NOT EXISTS idx_mv_lineage_task
    ON mv_llm_lineage(task_id, sequence_no);

CREATE INDEX IF NOT EXISTS idx_mv_lineage_model
    ON mv_llm_lineage(model, created_at DESC);


-- ─────────────────────────────────────────────
-- 7. HELPER FUNCTION: refresh_projections()
--
-- Called by the APScheduler in orchestrator_core.py
-- to refresh all materialized views concurrently (no locks).
-- ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION refresh_projections()
RETURNS void
LANGUAGE plpgsql AS
$$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_task_timeline;
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_task_projection;
    REFRESH MATERIALIZED VIEW CONCURRENTLY mv_llm_lineage;
END;
$$;


-- ─────────────────────────────────────────────
-- 8. STANDARD EVENT TYPES REFERENCE (comment only)
--
-- System:        agent_spawned, agent_idle, agent_shutdown
-- Task lifecycle: task_created, task_queued, task_claimed, task_started,
--                 task_completed, task_failed, task_cancelled,
--                 task_dead_lettered, task_retry_scheduled,
--                 task_stale_recovered, task_backoff_set
-- AI decisions:  llm_request_started, llm_request_finished, llm_request_failed,
--                prompt_generated, response_received
-- Code ops:      patch_generated, patch_applied, patch_rejected,
--                test_run_started, test_run_passed, test_run_failed
-- Review:        review_started, review_score_received,
--                review_passed, review_failed, review_fix_requested
-- Infra:         heartbeat, watchdog_triggered, stale_detected,
--                backoff_set, dep_cascade_cancelled
-- ─────────────────────────────────────────────
