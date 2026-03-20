-- 008_aes_metrics.sql
-- Persistence for AES Seniority metrics (Módulos 2 e 3)

-- 1. Track autonomous decisions
CREATE TABLE IF NOT EXISTS task_autonomous_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id),
    tenant_id TEXT NOT NULL,
    action_type TEXT NOT NULL, -- switch_agent, auto_decompose, inject_pitfalls, etc.
    priority_level INTEGER,
    applied_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 2. Track simulation evaluations
CREATE TABLE IF NOT EXISTS simulation_evaluations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id),
    tenant_id TEXT NOT NULL,
    predicted_success FLOAT NOT NULL,
    actual_success FLOAT, -- filled after execution
    strategy_name TEXT,
    error_delta FLOAT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3. Enhance goals table
ALTER TABLE goals ADD COLUMN IF NOT EXISTS constraints JSONB DEFAULT '[]';
ALTER TABLE goals ADD COLUMN IF NOT EXISTS deadline_hint TEXT;
ALTER TABLE goals ADD COLUMN IF NOT EXISTS task_dag_id UUID;

CREATE INDEX IF NOT EXISTS idx_actions_task ON task_autonomous_actions(task_id);
CREATE INDEX IF NOT EXISTS idx_evaluations_task ON simulation_evaluations(task_id);
