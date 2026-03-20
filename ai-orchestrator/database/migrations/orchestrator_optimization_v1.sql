-- SINC Orchestrator Optimization Migration
-- Sprint 1: Intelligence Foundation

-- Index for rapid task status monitoring
CREATE INDEX IF NOT EXISTS idx_tasks_status_created ON tasks(status, created_at DESC);

-- Index for agent performance analytics
CREATE INDEX IF NOT EXISTS idx_agent_events_agent_type_created ON agent_events(agent_name, event_type, created_at DESC);

-- Index for heartbeat ETA calculations
CREATE INDEX IF NOT EXISTS idx_heartbeats_task_beat ON heartbeats(task_id, beat_at DESC);

-- Index for reputation confidence tracking
CREATE INDEX IF NOT EXISTS idx_agent_reputation_stats ON agent_reputation(is_statistically_valid, reputation_fit_score DESC);

-- Index for usage cost analysis
CREATE INDEX IF NOT EXISTS idx_usage_log_day_cost ON usage_log(created_at, cost_usd DESC);
