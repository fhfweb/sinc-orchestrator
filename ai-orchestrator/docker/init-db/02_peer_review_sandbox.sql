-- SINC Orchestrator Schema — Migration 02
-- Peer-Review Workflow + Sandbox Execution
-- Safe: uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS

-- ─────────────────────────────────────────────
-- Peer-review columns on tasks
-- ─────────────────────────────────────────────

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS requires_review  BOOLEAN      DEFAULT FALSE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS review_feedback  TEXT;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reviewed_by      VARCHAR(100);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS reviewed_at      TIMESTAMP;

-- Index for quickly finding tasks awaiting review
CREATE INDEX IF NOT EXISTS idx_tasks_awaiting_review
    ON tasks(tenant_id, status)
    WHERE status = 'awaiting-review';

-- ─────────────────────────────────────────────
-- Sandbox execution table
-- Agents request sandbox runs here; workers execute and post results back.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS sandbox_executions (
    id           BIGSERIAL    PRIMARY KEY,
    task_id      VARCHAR(100) NOT NULL,
    tenant_id    TEXT         NOT NULL,
    agent_name   VARCHAR(100),
    script       TEXT         NOT NULL,
    working_dir  TEXT         DEFAULT '',
    status       TEXT         DEFAULT 'pending',  -- pending | running | passed | failed
    output       TEXT,
    exit_code    INTEGER,
    created_at   TIMESTAMPTZ  DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_sandbox_task    ON sandbox_executions(task_id);
CREATE INDEX IF NOT EXISTS idx_sandbox_status  ON sandbox_executions(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sandbox_tenant  ON sandbox_executions(tenant_id, created_at DESC);

-- ─────────────────────────────────────────────
-- Dead-letter index (tasks that exhausted all retries)
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_tasks_dead_letter
    ON tasks(tenant_id, updated_at DESC)
    WHERE status = 'dead-letter';
