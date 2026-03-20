-- Init 12: Indexes for dependency graph queries (Phase 1.2 / 2.3)
-- See database/migrations/005_dep_graph_indexes.sql for full rationale.

CREATE INDEX IF NOT EXISTS idx_dep_task_id
    ON dependencies(task_id);

CREATE INDEX IF NOT EXISTS idx_dep_task_dep
    ON dependencies(task_id, dependency_id);

CREATE INDEX IF NOT EXISTS idx_dep_dependency_id
    ON dependencies(dependency_id);
