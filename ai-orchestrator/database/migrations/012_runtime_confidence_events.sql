-- 012_runtime_confidence_events.sql
-- Persistence for runtime confidence degradation events (Sprint 4)

CREATE TABLE IF NOT EXISTS runtime_confidence_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL REFERENCES tasks(id),
    tenant_id TEXT NOT NULL,
    from_level TEXT,
    to_level TEXT,
    trigger TEXT NOT NULL,
    action_taken TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_rce_task ON runtime_confidence_events(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rce_tenant ON runtime_confidence_events(tenant_id, created_at DESC);
