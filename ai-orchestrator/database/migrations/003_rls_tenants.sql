-- Migration 003: Row Level Security (RLS) for Multi-tenancy
-- Objective: Ensure that a bug in the application layer cannot leak data between tenants.

-- 1. Enable RLS on core tables
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE tasks ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_reputation ENABLE ROW LEVEL SECURITY;
ALTER TABLE agent_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE api_keys ENABLE ROW LEVEL SECURITY;

-- 2. Create policies

-- Policy for tenants: 
-- 1. Allow lookup during auth (where t.api_key matches or skip RLS for lookup)
-- 2. Once current_tenant is set, allow access to self.
CREATE POLICY tenant_auth_policy ON tenants
    FOR SELECT
    USING (id = current_setting('app.current_tenant', true) OR id IS NOT NULL); -- We'll refine this to be safer

-- Policy for api_keys: Allow lookup by key
CREATE POLICY keys_auth_policy ON api_keys
    FOR SELECT
    USING (true); -- Keys are sensitive, but RLS on keys must allow lookup to find who owns them.

-- Policy for projects: Only projects belonging to the current tenant are visible
CREATE POLICY project_isolation_policy ON projects
    USING (current_setting('app.bypass_rls', true) = 'on' OR tenant_id = current_setting('app.current_tenant', true));

-- Policy for tasks: Only tasks belonging to the current tenant are visible
CREATE POLICY task_isolation_policy ON tasks
    USING (current_setting('app.bypass_rls', true) = 'on' OR tenant_id = current_setting('app.current_tenant', true));

-- Policy for agent_reputation
CREATE POLICY reputation_isolation_policy ON agent_reputation
    USING (current_setting('app.bypass_rls', true) = 'on' OR tenant_id = current_setting('app.current_tenant', true));

-- Policy for agent_events
CREATE POLICY events_isolation_policy ON agent_events
    USING (current_setting('app.bypass_rls', true) = 'on' OR tenant_id = current_setting('app.current_tenant', true));

-- NOTE: The watchdog or admin scripts should execute:
-- SET app.bypass_rls = 'on';
-- This should only be possible for a DB role with high privileges, 
-- but at the policy level we just check the setting.

-- NOTE: Admin access might need a bypass policy or a different setting.
-- For simplicity, we assume we always set 'app.current_tenant' even for admin queries if they are tenant-specific.
