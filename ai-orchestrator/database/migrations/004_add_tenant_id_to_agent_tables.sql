-- Migration 004: Add tenant_id to Agent tables
-- Supporting Row Level Security (RLS) for agent metrics.

-- 1. Heartbeats
ALTER TABLE heartbeats ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE heartbeats h SET tenant_id = t.tenant_id FROM tasks t WHERE h.task_id = t.id;
ALTER TABLE heartbeats ALTER COLUMN tenant_id SET NOT NULL;

-- 2. Webhook Dispatches
ALTER TABLE webhook_dispatches ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE webhook_dispatches wd SET tenant_id = t.tenant_id FROM tasks t WHERE wd.task_id = t.id;
ALTER TABLE webhook_dispatches ALTER COLUMN tenant_id SET NOT NULL;

-- 3. Agent Events
ALTER TABLE agent_events ADD COLUMN IF NOT EXISTS tenant_id TEXT;
UPDATE agent_events ae SET tenant_id = t.tenant_id FROM tasks t WHERE ae.task_id = t.id;
-- Note: Some agent_events might not be task-specific, but for SINC most are.
-- AE for non-task might need a default tenant or be system-level.

-- 4. Agent Reputation
-- This is tricky. Are agents global or per-tenant?
-- Audit implies isolation. Let's make reputation per-tenant.
-- We might need a composite primary key: (agent_name, tenant_id)
ALTER TABLE agent_reputation ADD COLUMN IF NOT EXISTS tenant_id TEXT;
-- Backfill with 'local' or 'sinc-tenant' if empty, but better to reset for fresh RLS.
UPDATE agent_reputation SET tenant_id = 'sinc-tenant' WHERE tenant_id IS NULL;
ALTER TABLE agent_reputation ALTER COLUMN tenant_id SET NOT NULL;

-- Drop and recreate PK for agent_reputation to include tenant_id
ALTER TABLE agent_reputation DROP CONSTRAINT IF EXISTS agent_reputation_pkey;
ALTER TABLE agent_reputation ADD PRIMARY KEY (agent_name, tenant_id);
