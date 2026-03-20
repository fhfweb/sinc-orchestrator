-- SINC Orchestrator Schema — Migration 04
-- Task Graph Execution Engine
-- Safe: uses IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
--
-- Adds:
--   * idx_tasks_blocked_deps  — fast scheduler scan for dependency-blocked tasks
--   * v_task_graph            — convenience view: task + its upstream dependency IDs
-- The `dependencies` table already exists (created in migration 01/02).
-- The `blocked-deps` status is handled at application level (no CHECK constraint).

-- ─────────────────────────────────────────────
-- Index: quickly find tasks waiting on deps
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_tasks_blocked_deps
    ON tasks(tenant_id, updated_at DESC)
    WHERE status = 'blocked-deps';

-- ─────────────────────────────────────────────
-- Index: quickly find root tasks of a plan (no deps)
-- ─────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_tasks_plan_status
    ON tasks(plan_id, status)
    WHERE plan_id != '';

-- ─────────────────────────────────────────────
-- View: task graph — each task with its deps as an array
-- Useful for DAG queries without application-level joins
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW v_task_graph AS
    SELECT
        t.id,
        t.title,
        t.status,
        t.assigned_agent,
        t.priority,
        t.plan_id,
        t.tenant_id,
        t.created_at,
        COALESCE(
            array_agg(d.dependency_id) FILTER (WHERE d.dependency_id IS NOT NULL),
            ARRAY[]::varchar[]
        ) AS depends_on
    FROM tasks t
    LEFT JOIN dependencies d ON d.task_id = t.id
    GROUP BY t.id;
