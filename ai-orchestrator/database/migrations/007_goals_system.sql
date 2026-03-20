-- 007_goals_system.sql
-- Persistence for high-level missions (Goals)

CREATE TABLE IF NOT EXISTS goals (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id TEXT NOT NULL,
    project_id TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planning', -- planning, executing, validating, done, failed
    acceptance_criteria JSONB DEFAULT '[]',
    metadata JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Link tasks to goals
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS goal_id UUID REFERENCES goals(id);

CREATE INDEX IF NOT EXISTS idx_goals_tenant ON goals(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tasks_goal ON tasks(goal_id);
