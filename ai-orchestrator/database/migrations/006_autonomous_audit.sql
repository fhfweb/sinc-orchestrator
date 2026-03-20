-- Migration: 006_autonomous_audit.sql
-- Description: Task audit tables for autonomous decisions and cognitive performance.

CREATE TABLE IF NOT EXISTS cognitive_executions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    task_category TEXT,
    resolved_at_layer TEXT, -- L0, L1, L2, L3, L4, LLM
    tokens_used INTEGER DEFAULT 0,
    tokens_saved INTEGER DEFAULT 0,
    latency_ms FLOAT,
    success BOOLEAN,
    error_type TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS task_autonomous_actions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    action_type TEXT NOT NULL, -- auto_decomposed, strict_mode, agent_switched, etc.
    reasoning TEXT,
    impact TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Index for analytics
CREATE INDEX idx_cognitive_executions_tenant_cat ON cognitive_executions(tenant_id, task_category);
CREATE INDEX idx_task_autonomous_actions_task ON task_autonomous_actions(task_id);
