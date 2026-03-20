-- SINC Orchestrator Schema — Migration 06
-- Digital Twin sync tracking (PostgreSQL side)
-- Safe: all IF NOT EXISTS

-- ─────────────────────────────────────────────
-- Tracks each full or incremental twin sync run.
-- Neo4j holds the actual graph; this table gives
-- us auditability, timing, and idempotency.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS twin_sync_log (
    id              BIGSERIAL    PRIMARY KEY,
    project_id      VARCHAR(100) NOT NULL DEFAULT '',
    tenant_id       VARCHAR(100) NOT NULL DEFAULT '',
    sync_type       VARCHAR(20)  NOT NULL DEFAULT 'full',   -- full | file | infra | task
    status          VARCHAR(20)  NOT NULL DEFAULT 'running', -- running | done | error
    files_scanned   INTEGER      DEFAULT 0,
    test_files      INTEGER      DEFAULT 0,
    nodes_created   INTEGER      DEFAULT 0,
    edges_created   INTEGER      DEFAULT 0,
    infra_services  INTEGER      DEFAULT 0,
    errors          INTEGER      DEFAULT 0,
    detail          JSONB        DEFAULT '{}',
    started_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    finished_at     TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_twin_sync_project
    ON twin_sync_log(tenant_id, project_id, started_at DESC);

CREATE INDEX IF NOT EXISTS idx_twin_sync_status
    ON twin_sync_log(status, started_at DESC);

-- ─────────────────────────────────────────────
-- task_file_links: PostgreSQL mirror of
-- (:OrchestratorTask)-[:MODIFIES]->(:File) edges.
-- Enables fast SQL queries without Neo4j round-trips.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS task_file_links (
    task_id     VARCHAR(100) NOT NULL,
    file_path   VARCHAR(500) NOT NULL,
    project_id  VARCHAR(100) NOT NULL DEFAULT '',
    tenant_id   VARCHAR(100) NOT NULL DEFAULT '',
    linked_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (task_id, file_path)
);

CREATE INDEX IF NOT EXISTS idx_task_file_links_file
    ON task_file_links(file_path, tenant_id);

CREATE INDEX IF NOT EXISTS idx_task_file_links_task
    ON task_file_links(task_id);
