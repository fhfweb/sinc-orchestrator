-- SINC Orchestrator Schema — Migration 07
-- Entropy Scanner: per-file health snapshots + trend analysis
-- Safe: all IF NOT EXISTS

-- ─────────────────────────────────────────────
-- One row per file per scan run.
-- Querying DISTINCT ON (file_path) ORDER BY scan_at DESC
-- gives you the latest snapshot for every file.
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS entropy_snapshots (
    id              BIGSERIAL    PRIMARY KEY,
    project_id      VARCHAR(100) NOT NULL DEFAULT '',
    tenant_id       VARCHAR(100) NOT NULL DEFAULT '',
    file_path       VARCHAR(500) NOT NULL,
    language        VARCHAR(20)  DEFAULT '',
    entropy_score   NUMERIC(5,4) NOT NULL DEFAULT 0,
    label           VARCHAR(20)  NOT NULL DEFAULT 'healthy', -- healthy|watch|refactor|critical|structural_hazard
    complexity      INTEGER      DEFAULT 0,   -- cyclomatic complexity
    max_fn_lines    INTEGER      DEFAULT 0,   -- longest function (lines)
    file_lines      INTEGER      DEFAULT 0,   -- total file lines
    fn_count        INTEGER      DEFAULT 0,   -- number of functions/methods
    coupling        INTEGER      DEFAULT 0,   -- reverse-import count
    test_coverage   NUMERIC(4,3) DEFAULT 0,   -- 0.0–1.0
    circular_deps   BOOLEAN      DEFAULT FALSE,
    duplication     NUMERIC(4,3) DEFAULT 0,   -- fraction of duplicated lines
    metrics_json    JSONB        DEFAULT '{}',
    scan_at         TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- Latest snapshot per file (most common query)
CREATE INDEX IF NOT EXISTS idx_entropy_project_file_time
    ON entropy_snapshots(project_id, tenant_id, file_path, scan_at DESC);

-- Time-series queries (project trend)
CREATE INDEX IF NOT EXISTS idx_entropy_project_time
    ON entropy_snapshots(project_id, tenant_id, scan_at DESC);

-- High-entropy scan (find worst offenders)
CREATE INDEX IF NOT EXISTS idx_entropy_score
    ON entropy_snapshots(project_id, tenant_id, entropy_score DESC);

-- Label-based filter
CREATE INDEX IF NOT EXISTS idx_entropy_label
    ON entropy_snapshots(project_id, tenant_id, label, scan_at DESC);

-- ─────────────────────────────────────────────
-- Convenience view: latest entropy per file
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW v_entropy_latest AS
    SELECT DISTINCT ON (project_id, tenant_id, file_path)
           id, project_id, tenant_id, file_path, language,
           entropy_score, label,
           complexity, max_fn_lines, file_lines, fn_count,
           coupling, test_coverage, circular_deps, duplication,
           scan_at
    FROM entropy_snapshots
    ORDER BY project_id, tenant_id, file_path, scan_at DESC;

-- ─────────────────────────────────────────────
-- Convenience view: project-level health summary
-- (average entropy per scan_hour for trend charts)
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW v_entropy_project_trend AS
    SELECT
        project_id,
        tenant_id,
        DATE_TRUNC('hour', scan_at) AS scan_hour,
        ROUND(AVG(entropy_score)::NUMERIC, 4)                        AS avg_entropy,
        COUNT(*) FILTER (WHERE label = 'structural_hazard')         AS structural_hazard_count,
        COUNT(*) FILTER (WHERE label = 'critical')                  AS critical_count,
        COUNT(*) FILTER (WHERE label = 'refactor')                  AS refactor_count,
        COUNT(*) FILTER (WHERE label = 'watch')                     AS watch_count,
        COUNT(*) FILTER (WHERE label = 'healthy')                   AS healthy_count,
        COUNT(*)                                                     AS total_files
    FROM entropy_snapshots
    GROUP BY project_id, tenant_id, DATE_TRUNC('hour', scan_at)
    ORDER BY scan_hour DESC;
