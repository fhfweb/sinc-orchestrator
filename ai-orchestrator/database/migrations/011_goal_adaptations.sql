-- 011_goal_adaptations.sql
-- Results from live goal adaptations (Sprint 3)

CREATE TABLE IF NOT EXISTS goal_adaptations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    goal_id UUID NOT NULL REFERENCES goals(id),
    tenant_id TEXT NOT NULL,
    adaptation_type TEXT NOT NULL, -- reorder, cancel_subtask, add_subtask, escalate
    affected_tasks JSONB,
    reason TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_goal_adaptations_goal ON goal_adaptations(goal_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_goal_adaptations_tenant ON goal_adaptations(tenant_id);
