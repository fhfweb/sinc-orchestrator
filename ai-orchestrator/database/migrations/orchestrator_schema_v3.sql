-- SINC Orchestrator Schema V3
-- Entropy Scanner v3 — Z-score model, structural_hazard label, new metrics
-- Safe migration: uses ALTER TABLE ADD COLUMN IF NOT EXISTS
-- Apply on top of v2 for existing databases.

-- ─────────────────────────────────────────────
-- entropy_snapshots: add v3 metric columns
-- (new fields complement the existing metrics_json JSONB)
-- ─────────────────────────────────────────────

ALTER TABLE entropy_snapshots
    ADD COLUMN IF NOT EXISTS instability   NUMERIC(5,4) DEFAULT 1.0,
    ADD COLUMN IF NOT EXISTS blast_weight  NUMERIC(5,4) DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS dep_entropy   NUMERIC(5,4) DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS hotspot_score NUMERIC(5,4) DEFAULT 0.0,
    ADD COLUMN IF NOT EXISTS martin_zone   VARCHAR(20)  DEFAULT 'neutral',
    ADD COLUMN IF NOT EXISTS churn_count   INTEGER      DEFAULT 0;

-- Extend label column to accommodate the new structural_hazard value.
-- VARCHAR(20) already fits — this is a comment/index update only.

-- Index: hotspot queries (highest churn × highest entropy)
CREATE INDEX IF NOT EXISTS idx_entropy_hotspot
    ON entropy_snapshots(project_id, tenant_id, hotspot_score DESC);

-- ─────────────────────────────────────────────
-- Rebuild trend view to include structural_hazard
-- ─────────────────────────────────────────────

CREATE OR REPLACE VIEW v_entropy_project_trend AS
    SELECT
        project_id,
        tenant_id,
        DATE_TRUNC('hour', scan_at) AS scan_hour,
        ROUND(AVG(entropy_score)::NUMERIC, 4)                        AS avg_entropy,
        COUNT(*) FILTER (WHERE label = 'structural_hazard')          AS structural_hazard_count,
        COUNT(*) FILTER (WHERE label = 'critical')                   AS critical_count,
        COUNT(*) FILTER (WHERE label = 'refactor')                   AS refactor_count,
        COUNT(*) FILTER (WHERE label = 'watch')                      AS watch_count,
        COUNT(*) FILTER (WHERE label = 'healthy')                    AS healthy_count,
        COUNT(*)                                                      AS total_files
    FROM entropy_snapshots
    GROUP BY project_id, tenant_id, DATE_TRUNC('hour', scan_at)
    ORDER BY scan_hour DESC;

-- Rebuild latest view to expose new columns
CREATE OR REPLACE VIEW v_entropy_latest AS
    SELECT DISTINCT ON (project_id, tenant_id, file_path)
           id, project_id, tenant_id, file_path, language,
           entropy_score, label,
           complexity, max_fn_lines, file_lines, fn_count,
           coupling, test_coverage, circular_deps, duplication,
           instability, blast_weight, dep_entropy, hotspot_score,
           martin_zone, churn_count,
           scan_at
    FROM entropy_snapshots
    ORDER BY project_id, tenant_id, file_path, scan_at DESC;
