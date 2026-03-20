-- SINC Orchestrator Schema — Migration 05
-- Performance indexes + correctness fixes from audit
-- Safe: all IF NOT EXISTS

-- ─────────────────────────────────────────────
-- Index: reverse lookup on dependencies
-- Needed by _resolve_dependencies() which queries
--   "who depends ON this completed task?"
-- Without this: full seq-scan on every task completion.
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_dependencies_dep_id
    ON dependencies(dependency_id);

-- ─────────────────────────────────────────────
-- Index: tasks by (tenant, status) — used by watchdog
-- and all status-filtered queries.
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_tasks_tenant_status
    ON tasks(tenant_id, status);

-- ─────────────────────────────────────────────
-- Index: plans by (tenant, status) — used by watchdog
-- plan completion scan
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_plans_tenant_status
    ON plans(tenant_id, status);

-- ─────────────────────────────────────────────
-- Index: webhook_dispatches by (task_id, status)
-- Used by agent_pending cleanup of stale dispatches
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_dispatches_task_status
    ON webhook_dispatches(task_id, status);
