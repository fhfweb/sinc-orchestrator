-- SINC Orchestrator Schema — Migration 09
-- GitHub Connector: connected repos, snapshots, webhook events
-- Safe: all IF NOT EXISTS

-- ─────────────────────────────────────────────
-- One row per connected repository.
-- id = md5(tenant_id:project_id) — stable across reconnects.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS connected_repos (
    id              VARCHAR(20)  PRIMARY KEY,
    repo_url        TEXT         NOT NULL,
    project_id      VARCHAR(100) NOT NULL DEFAULT '',
    tenant_id       VARCHAR(100) NOT NULL DEFAULT '',
    branch          VARCHAR(100) NOT NULL DEFAULT 'main',
    clone_path      TEXT,
    stack           JSONB        DEFAULT '{}',
    webhook_secret  VARCHAR(200) DEFAULT '',
    sync_status     VARCHAR(20)  DEFAULT 'pending',  -- connecting|done|error|syncing
    connected_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    last_sync_at    TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_repos_tenant
    ON connected_repos(tenant_id, connected_at DESC);

CREATE INDEX IF NOT EXISTS idx_repos_project
    ON connected_repos(project_id, tenant_id);

-- ─────────────────────────────────────────────
-- One row per analysis snapshot (v1, v2 after each sync).
-- Keeps the full twin + entropy summary for trend analysis.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS repo_snapshots (
    id              VARCHAR(20)  PRIMARY KEY,
    project_id      VARCHAR(100) NOT NULL DEFAULT '',
    tenant_id       VARCHAR(100) NOT NULL DEFAULT '',
    clone_path      TEXT,
    twin_stats      JSONB        DEFAULT '{}',
    entropy_summary JSONB        DEFAULT '{}',
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_snapshots_project_time
    ON repo_snapshots(project_id, tenant_id, created_at DESC);

-- ─────────────────────────────────────────────
-- GitHub webhook event log.
-- Stores raw payloads + simulation results for PR events.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS webhook_events (
    id              BIGSERIAL    PRIMARY KEY,
    tenant_id       VARCHAR(100) NOT NULL DEFAULT '',
    event_type      VARCHAR(50)  NOT NULL DEFAULT '',  -- pull_request|push|release
    repo_full_name  VARCHAR(200) DEFAULT '',           -- owner/repo
    pr_number       INTEGER,
    head_sha        VARCHAR(60)  DEFAULT '',
    risk_score      NUMERIC(5,4),
    risk_label      VARCHAR(20),
    recommendation  VARCHAR(30),
    payload         JSONB        DEFAULT '{}',
    result          JSONB        DEFAULT '{}',
    received_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_webhook_events_tenant_time
    ON webhook_events(tenant_id, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_webhook_events_repo
    ON webhook_events(repo_full_name, received_at DESC);

CREATE INDEX IF NOT EXISTS idx_webhook_events_pr
    ON webhook_events(repo_full_name, pr_number, received_at DESC);

-- ─────────────────────────────────────────────
-- Convenience view: latest snapshot per project
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW v_latest_snapshots AS
    SELECT DISTINCT ON (project_id, tenant_id)
           id, project_id, tenant_id, clone_path,
           twin_stats, entropy_summary, created_at
    FROM repo_snapshots
    ORDER BY project_id, tenant_id, created_at DESC;

-- ─────────────────────────────────────────────
-- Convenience view: repo health summary
-- Joins connected_repo + latest snapshot + latest entropy average
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW v_repo_health AS
    SELECT
        r.project_id,
        r.tenant_id,
        r.repo_url,
        r.branch,
        r.sync_status,
        r.stack->>'primary'                               AS primary_language,
        r.last_sync_at,
        s.created_at                                      AS last_snapshot_at,
        (s.entropy_summary->>'avg_entropy')::NUMERIC      AS avg_entropy,
        (s.entropy_summary->>'critical')::INTEGER         AS critical_files,
        (s.entropy_summary->>'files_scanned')::INTEGER    AS files_scanned,
        (s.twin_stats->>'files')::INTEGER                 AS twin_files
    FROM connected_repos r
    LEFT JOIN v_latest_snapshots s
        ON s.project_id = r.project_id AND s.tenant_id = r.tenant_id;
