-- SINC Orchestrator Schema — Migration 13
-- Intelligence Sprint: indexes, ETA tracking, predictive analytics
--
-- What this migration adds:
--   1. agent_reputation(tenant_id) index   — missing from 004_add_tenant_id
--   2. heartbeats.progress_pct column       — for ETA estimation via linear regression
--   3. heartbeats(task_id, updated_at) index — time-ordered heartbeat queries
--   4. task_success_prediction materialized view — predictive analytics
--   5. agent_reputation.semantic_score column — semantic leaderboard support
-- ─────────────────────────────────────────────────────────────────────────────


-- ─────────────────────────────────────────────
-- 1. agent_reputation — tenant_id index
-- ─────────────────────────────────────────────
-- The PK is (agent_name, tenant_id) but filtering "all agents for tenant X"
-- requires a leading tenant_id index; the PK scan is unordered by tenant.

CREATE INDEX IF NOT EXISTS idx_agent_reputation_tenant
    ON agent_reputation (tenant_id);

COMMENT ON INDEX idx_agent_reputation_tenant IS
    'Supports GET /agents and /agents/recommend which filter by tenant_id';


-- ─────────────────────────────────────────────
-- 2. heartbeats.progress_pct
-- ─────────────────────────────────────────────
-- Added for ETA estimation: the agent upserts its progress (0.0–1.0)
-- on each heartbeat so a linear regression over (updated_at, progress_pct)
-- can predict time-to-completion.

ALTER TABLE heartbeats
    ADD COLUMN IF NOT EXISTS progress_pct NUMERIC(5,4) DEFAULT NULL;

COMMENT ON COLUMN heartbeats.progress_pct IS
    'Agent-reported progress 0.0–1.0, updated each heartbeat. '
    'Used by estimate_task_completion() for linear regression ETA.';


-- ─────────────────────────────────────────────
-- 3. heartbeats (task_id, updated_at) index
-- ─────────────────────────────────────────────
-- ETA regression queries: WHERE task_id = $1 ORDER BY updated_at
-- The PK covers task_id but not the time-sort needed for regression.

CREATE INDEX IF NOT EXISTS idx_heartbeats_task_time
    ON heartbeats (task_id, updated_at);

COMMENT ON INDEX idx_heartbeats_task_time IS
    'Supports estimate_task_completion() linear regression over heartbeat history';


-- ─────────────────────────────────────────────
-- 4. agent_reputation.semantic_score
-- ─────────────────────────────────────────────
-- Redis leaderboard stores the live EMA score; this column caches the
-- last flushed value for SQL-based queries and /agents/recommend.

ALTER TABLE agent_reputation
    ADD COLUMN IF NOT EXISTS semantic_score NUMERIC(6,4) DEFAULT 0.5;

COMMENT ON COLUMN agent_reputation.semantic_score IS
    'EMA-smoothed score from Redis agent leaderboard (ZADD). '
    'Updated by update_agent_leaderboard() after each task completion. '
    'Range: 0.0 (all failures) to 1.0 (all successes).';

CREATE INDEX IF NOT EXISTS idx_agent_reputation_semantic
    ON agent_reputation (tenant_id, semantic_score DESC);

COMMENT ON INDEX idx_agent_reputation_semantic IS
    'Supports /agents/recommend ORDER BY semantic_score DESC LIMIT K';


-- ─────────────────────────────────────────────
-- 5. task_success_prediction materialized view
-- ─────────────────────────────────────────────
-- Pre-aggregated success rates and durations per (task_type, agent, tenant).
-- Refreshed by a background job every 5 minutes.
-- Powers GET /analytics/intelligence and /agents/recommend.

CREATE MATERIALIZED VIEW IF NOT EXISTS task_success_prediction AS
SELECT
    t.tenant_id,
    t.task_type,
    ae.agent_name,
    COUNT(*)                                                        AS sample_count,
    ROUND(
        AVG(CASE WHEN t.status = 'done' THEN 1.0 ELSE 0.0 END)::NUMERIC,
        4
    )                                                               AS success_rate,
    ROUND(AVG(
        EXTRACT(EPOCH FROM (t.updated_at - t.created_at)) * 1000
    )::NUMERIC, 0)                                                  AS avg_duration_ms,
    ROUND(PERCENTILE_CONT(0.90) WITHIN GROUP (
        ORDER BY EXTRACT(EPOCH FROM (t.updated_at - t.created_at)) * 1000
    )::NUMERIC, 0)                                                  AS p90_duration_ms
FROM tasks t
JOIN agent_events ae
    ON ae.task_id = t.id::text
    AND ae.event_type = 'complete'
WHERE t.status IN ('done', 'failed')
  AND t.created_at > NOW() - INTERVAL '30 days'
GROUP BY t.tenant_id, t.task_type, ae.agent_name
HAVING COUNT(*) >= 3;

CREATE UNIQUE INDEX IF NOT EXISTS idx_task_success_pred_pk
    ON task_success_prediction (tenant_id, task_type, agent_name);

CREATE INDEX IF NOT EXISTS idx_task_success_pred_agent
    ON task_success_prediction (tenant_id, agent_name, success_rate DESC);

COMMENT ON MATERIALIZED VIEW task_success_prediction IS
    'Pre-computed success rates and durations per (tenant, task_type, agent). '
    'REFRESH MATERIALIZED VIEW CONCURRENTLY task_success_prediction; -- every 5 min';
