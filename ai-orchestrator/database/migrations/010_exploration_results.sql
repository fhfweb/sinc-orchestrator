-- 010_exploration_results.sql
-- Results from exploratory (challenger) strategies

CREATE TABLE IF NOT EXISTS experimental_strategy_results (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id),
    tenant_id TEXT NOT NULL,
    strategy_name TEXT NOT NULL,
    agent_used TEXT NOT NULL,
    predicted_success FLOAT,
    actual_success BOOLEAN,
    was_selected BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_exp_results_tenant ON experimental_strategy_results(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_exp_results_task ON experimental_strategy_results(task_id);
