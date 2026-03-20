-- Migration 005: Indexes for dependency graph queries
-- Supports WITH RECURSIVE cycle detection (_has_cycle) and
-- dependency resolution (_resolve_dependencies) added in Phase 1.2 / 2.3.
--
-- Safe: all statements use IF NOT EXISTS.

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. Recursive CTE traversal
--    Query pattern:  SELECT d.dependency_id FROM dependencies d
--                    INNER JOIN reachable r ON d.task_id = r.node
--    The recursive step looks up rows by task_id on every iteration.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_dep_task_id
    ON dependencies(task_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. Covering index for the full recursive join (task_id → dependency_id)
--    Allows index-only scans; avoids heap fetches in the hot recursive path.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_dep_task_dep
    ON dependencies(task_id, dependency_id);

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. Reverse lookup: "which tasks depend on this completed task?"
--    Used by _resolve_dependencies when a task transitions to 'done'.
-- ─────────────────────────────────────────────────────────────────────────────

CREATE INDEX IF NOT EXISTS idx_dep_dependency_id
    ON dependencies(dependency_id);
