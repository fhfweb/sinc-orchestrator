-- 009_level5_routing.sql
-- Persistence for Level 5 tiered execution routing

CREATE TABLE IF NOT EXISTS execution_routes (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    task_id UUID NOT NULL,
    tenant_id TEXT NOT NULL,
    execution_path TEXT NOT NULL, -- instant, fast, standard, deep
    reason TEXT,
    decided_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_routes_tenant_path ON execution_routes(tenant_id, execution_path, decided_at DESC);
CREATE INDEX IF NOT EXISTS idx_routes_task ON execution_routes(task_id);
