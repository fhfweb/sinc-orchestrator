-- SINC Orchestrator Intelligence Migration v2
-- Sprint 2: Learning Loop & Prediction

-- 0. Schema Fix: Add task_type if missing
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS task_type VARCHAR(100) DEFAULT 'generic';
CREATE INDEX IF NOT EXISTS idx_tasks_task_type ON tasks (task_type);

-- 1. Task Success Prediction Materialized View
-- Aggregates historical performance per tenant, task type, and agent.
CREATE MATERIALIZED VIEW IF NOT EXISTS task_success_prediction AS
SELECT 
    tenant_id,
    task_type,
    assigned_agent AS agent_name,
    COUNT(*) FILTER (WHERE status = 'done') / COUNT(*)::float AS success_rate,
    AVG(EXTRACT(EPOCH FROM (updated_at - started_at)) * 1000) FILTER (WHERE status = 'done') AS avg_duration_ms,
    PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY EXTRACT(EPOCH FROM (updated_at - started_at)) * 1000) FILTER (WHERE status = 'done') AS p90_duration_ms,
    COUNT(*) AS sample_count
FROM tasks
WHERE status IN ('done', 'failed', 'dead-letter')
  AND started_at IS NOT NULL
GROUP BY tenant_id, task_type, assigned_agent;

-- Unique index for fast lookups and concurrent refresh
CREATE UNIQUE INDEX IF NOT EXISTS idx_tsp_lookup ON task_success_prediction (tenant_id, task_type, agent_name);

-- 2. Heartbeat Analysis Index for ETA
-- Speeds up queries that calculate progress based on heartbeat frequency
CREATE INDEX IF NOT EXISTS idx_heartbeats_task_progression ON heartbeats (task_id, beat_at DESC);

-- 3. Function to refresh the view (can be called by watchdog)
CREATE OR REPLACE FUNCTION refresh_intelligence_views()
RETURNS void AS $$
BEGIN
    REFRESH MATERIALIZED VIEW CONCURRENTLY task_success_prediction;
END;
$$ LANGUAGE plpgsql;
