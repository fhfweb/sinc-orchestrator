-- SINC Orchestrator Schema — Migration 08
-- Engineering Time Machine: simulation runs + blast radius cache
-- Safe: all IF NOT EXISTS

-- ─────────────────────────────────────────────
-- Each row = one simulate_change() or simulate_task() call.
-- Stores the full result JSON for replay / audit.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS simulation_runs (
    id                  VARCHAR(20)  PRIMARY KEY,          -- hash of task_id + ts
    task_id             VARCHAR(100) NOT NULL DEFAULT '',
    task_title          TEXT         NOT NULL DEFAULT '',
    project_id          VARCHAR(100) NOT NULL DEFAULT '',
    tenant_id           VARCHAR(100) NOT NULL DEFAULT '',
    risk_score          NUMERIC(5,4) NOT NULL DEFAULT 0,
    risk_label          VARCHAR(20)  NOT NULL DEFAULT '',  -- low|medium|high|critical
    recommendation      VARCHAR(30)  NOT NULL DEFAULT '',  -- execute_now|split_task|...
    blast_files         INTEGER      DEFAULT 0,
    blast_tests         INTEGER      DEFAULT 0,
    broken_interfaces   INTEGER      DEFAULT 0,
    entropy_delta       NUMERIC(5,4) DEFAULT 0,
    pre_tasks           JSONB        DEFAULT '[]',
    full_result         JSONB        DEFAULT '{}',
    simulated_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- Query by project + time
CREATE INDEX IF NOT EXISTS idx_sim_project_time
    ON simulation_runs(project_id, tenant_id, simulated_at DESC);

-- Query by task
CREATE INDEX IF NOT EXISTS idx_sim_task
    ON simulation_runs(task_id, simulated_at DESC);

-- High-risk scan
CREATE INDEX IF NOT EXISTS idx_sim_risk
    ON simulation_runs(project_id, tenant_id, risk_score DESC);

-- ─────────────────────────────────────────────
-- Blast radius cache — avoids re-scanning the graph
-- for the same set of seed files within a TTL window.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS blast_radius_cache (
    cache_key           VARCHAR(64)  PRIMARY KEY,          -- md5(sorted seed files)
    project_id          VARCHAR(100) NOT NULL DEFAULT '',
    tenant_id           VARCHAR(100) NOT NULL DEFAULT '',
    seed_files          JSONB        NOT NULL DEFAULT '[]',
    affected_files      JSONB        NOT NULL DEFAULT '[]',
    affected_tests      JSONB        NOT NULL DEFAULT '[]',
    file_count          INTEGER      DEFAULT 0,
    test_count          INTEGER      DEFAULT 0,
    risk_score          NUMERIC(5,4) DEFAULT 0,
    computed_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    expires_at          TIMESTAMP    GENERATED ALWAYS AS
                            (computed_at + INTERVAL '1 hour') STORED
);

CREATE INDEX IF NOT EXISTS idx_blast_cache_project
    ON blast_radius_cache(project_id, tenant_id, computed_at DESC);

-- ─────────────────────────────────────────────
-- Convenience view: simulation summary per task
-- (latest simulation run per task_id)
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW v_simulation_latest AS
    SELECT DISTINCT ON (task_id)
           id, task_id, task_title, project_id, tenant_id,
           risk_score, risk_label, recommendation,
           blast_files, blast_tests, broken_interfaces,
           entropy_delta, simulated_at
    FROM simulation_runs
    ORDER BY task_id, simulated_at DESC;
