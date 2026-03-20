-- SINC Orchestrator Schema — Migration 03
-- Lessons Learned + Plans + Closed-Loop Verification + Red Team
-- Safe: uses IF NOT EXISTS / ADD COLUMN IF NOT EXISTS

-- ─────────────────────────────────────────────
-- Lessons Learned
-- Agents record error patterns and their fixes.
-- Future agents query this before executing to avoid known failure loops.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS lessons_learned (
    id              BIGSERIAL    PRIMARY KEY,
    tenant_id       TEXT         NOT NULL,
    project_id      TEXT         NOT NULL DEFAULT '',
    error_signature TEXT         NOT NULL,   -- normalized error key (e.g. "class-not-found")
    context         TEXT         DEFAULT '',  -- what was happening when the error occurred
    attempted_fix   TEXT         NOT NULL,   -- what was tried
    result          TEXT         NOT NULL,   -- 'success' | 'failure'
    confidence      FLOAT        DEFAULT 1.0,
    agent_name      VARCHAR(100) DEFAULT '',
    task_id         VARCHAR(100) DEFAULT '',
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_lessons_tenant ON lessons_learned(tenant_id, project_id);
CREATE INDEX IF NOT EXISTS idx_lessons_sig    ON lessons_learned(error_signature, result, confidence DESC);

-- ─────────────────────────────────────────────
-- Plans (Global Planner output)
-- Each plan is a decomposed goal → N tasks with dependencies.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS plans (
    id          VARCHAR(100) PRIMARY KEY,
    tenant_id   TEXT         NOT NULL,
    project_id  TEXT         NOT NULL DEFAULT '',
    goal        TEXT         NOT NULL,
    status      TEXT         DEFAULT 'active',   -- active | completed | cancelled
    task_count  INTEGER      DEFAULT 0,
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_plans_tenant ON plans(tenant_id, created_at DESC);

-- ─────────────────────────────────────────────
-- New columns on tasks
-- ─────────────────────────────────────────────

-- Closed-Loop Verification: agent cannot mark done without evidence of execution
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verification_required BOOLEAN DEFAULT FALSE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS verification_script   TEXT    DEFAULT '';

-- Red Team: auto-create an auditor task after agent completes
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS red_team_enabled  BOOLEAN      DEFAULT FALSE;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS red_team_task_id  VARCHAR(100) DEFAULT '';

-- Plan grouping
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS plan_id  VARCHAR(100) DEFAULT '';

-- ─────────────────────────────────────────────
-- Indexes for new states and plan grouping
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_tasks_awaiting_verification
    ON tasks(tenant_id, status)
    WHERE status = 'awaiting-verification';

CREATE INDEX IF NOT EXISTS idx_tasks_plan
    ON tasks(tenant_id, plan_id)
    WHERE plan_id != '';
