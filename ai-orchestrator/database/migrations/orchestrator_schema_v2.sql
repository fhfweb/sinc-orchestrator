-- SINC Orchestrator Schema V2
-- Target: PostgreSQL 17
-- Adds: heartbeat, webhook_dispatch, agent_events, lock_backoff, reputation_confidence
-- Safe migration: uses IF NOT EXISTS / ALTER TABLE ADD COLUMN IF NOT EXISTS

-- ─────────────────────────────────────────────
-- CORE TABLES (idempotent)
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS tenants (
    id          TEXT         PRIMARY KEY,
    name        TEXT         NOT NULL,
    api_key     TEXT         UNIQUE NOT NULL,
    plan        TEXT         DEFAULT 'free',     -- free | pro | enterprise
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS projects (
    id          TEXT         PRIMARY KEY,
    tenant_id   TEXT         REFERENCES tenants(id),
    name        TEXT         NOT NULL,
    repo_url    TEXT,
    stack       TEXT,
    status      TEXT         DEFAULT 'active',
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tasks (
    id              VARCHAR(100) PRIMARY KEY,
    project_id      VARCHAR(50)  REFERENCES projects(id),
    tenant_id       TEXT         REFERENCES tenants(id),
    title           TEXT         DEFAULT '',
    status          VARCHAR(50)  DEFAULT 'pending',
    assigned_agent  VARCHAR(100),
    description     TEXT,
    priority        VARCHAR(10)  DEFAULT 'P2',
    lock_ttl        INTEGER      DEFAULT 20,
    critical_path   BOOLEAN      DEFAULT FALSE,
    lock_conflict_count    INTEGER   DEFAULT 0,
    lock_retry_count       INTEGER   DEFAULT 0,
    lock_backoff_until     TIMESTAMP,
    lock_conflict_since    TIMESTAMP,
    created_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    started_at      TIMESTAMP,
    updated_at      TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    completed_at    TIMESTAMP,
    metadata        JSONB        DEFAULT '{}'
);

-- ─────────────────────────────────────────────
-- TABLE UPDATES (Safety for existing installations)
-- ─────────────────────────────────────────────

ALTER TABLE projects ADD COLUMN IF NOT EXISTS tenant_id TEXT REFERENCES tenants(id);
ALTER TABLE projects ADD COLUMN IF NOT EXISTS repo_url  TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS stack     TEXT;
ALTER TABLE projects ADD COLUMN IF NOT EXISTS status    TEXT DEFAULT 'active';
ALTER TABLE projects ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();

ALTER TABLE tasks ADD COLUMN IF NOT EXISTS title      TEXT DEFAULT '';
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS tenant_id  TEXT REFERENCES tenants(id);
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lock_conflict_count    INTEGER   DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lock_retry_count       INTEGER   DEFAULT 0;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lock_backoff_until     TIMESTAMP;
ALTER TABLE tasks ADD COLUMN IF NOT EXISTS lock_conflict_since    TIMESTAMP;

CREATE TABLE IF NOT EXISTS dependencies (
    task_id        VARCHAR(100) REFERENCES tasks(id) ON DELETE CASCADE,
    dependency_id  VARCHAR(100) REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, dependency_id)
);

-- ─────────────────────────────────────────────
-- V2: HEARTBEAT TABLE
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS heartbeats (
    task_id        VARCHAR(100) NOT NULL,
    agent_name     VARCHAR(100) NOT NULL,
    beat_at        TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    progress_pct   INTEGER      DEFAULT 0,
    current_step   TEXT,
    metadata       JSONB        DEFAULT '{}',
    PRIMARY KEY (task_id, agent_name)
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_beat_at ON heartbeats(beat_at);

-- ─────────────────────────────────────────────
-- V2: WEBHOOK DISPATCH TABLE
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS webhook_dispatches (
    id              SERIAL       PRIMARY KEY,
    task_id         VARCHAR(100) NOT NULL,
    agent_name      VARCHAR(100) NOT NULL,
    status          VARCHAR(20)  DEFAULT 'pending',   -- pending | delivered | completed | failed
    dispatch_payload JSONB       NOT NULL DEFAULT '{}',
    dispatched_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    delivered_at    TIMESTAMP,
    completed_at    TIMESTAMP,
    completion_payload JSONB     DEFAULT '{}',
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_dispatches_agent_status
    ON webhook_dispatches(agent_name, status);

CREATE INDEX IF NOT EXISTS idx_webhook_dispatches_task
    ON webhook_dispatches(task_id);

-- ─────────────────────────────────────────────
-- V2: AGENT EVENTS TABLE
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_events (
    id          BIGSERIAL    PRIMARY KEY,
    task_id     VARCHAR(100),
    agent_name  VARCHAR(100),
    event_type  VARCHAR(50)  NOT NULL,  -- dispatch | start | heartbeat | complete | fail | repair
    payload     JSONB        DEFAULT '{}',
    created_at  TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_agent_events_task   ON agent_events(task_id);
CREATE INDEX IF NOT EXISTS idx_agent_events_agent  ON agent_events(agent_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_events_type   ON agent_events(event_type, created_at DESC);

-- ─────────────────────────────────────────────
-- V2: REPUTATION TABLE
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS agent_reputation (
    agent_name              VARCHAR(100) PRIMARY KEY,
    backend_affinity        NUMERIC(5,3) DEFAULT 0.1,
    frontend_affinity       NUMERIC(5,3) DEFAULT 0.1,
    db_affinity             NUMERIC(5,3) DEFAULT 0.1,
    arch_affinity           NUMERIC(5,3) DEFAULT 0.1,
    qa_affinity             NUMERIC(5,3) DEFAULT 0.1,
    devops_affinity         NUMERIC(5,3) DEFAULT 0.1,
    tasks_total             INTEGER      DEFAULT 0,
    tasks_success           INTEGER      DEFAULT 0,
    tasks_failure           INTEGER      DEFAULT 0,
    runtime_success_rate    NUMERIC(5,4) DEFAULT 0.5,
    runtime_avg_duration_ms INTEGER      DEFAULT 0,
    runtime_timeout_rate    NUMERIC(5,4) DEFAULT 0.0,
    reputation_fit_score    NUMERIC(5,4) DEFAULT 0.5,
    runtime_samples         INTEGER      DEFAULT 0,
    confidence_lower        NUMERIC(5,4) DEFAULT 0.0,
    confidence_upper        NUMERIC(5,4) DEFAULT 1.0,
    confidence_level        VARCHAR(10)  DEFAULT 'low',
    is_statistically_valid  BOOLEAN      DEFAULT FALSE,
    updated_at              TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────────────────────
-- V2: HUMAN GATES TABLE
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS human_gates (
    id              SERIAL       PRIMARY KEY,
    task_id         VARCHAR(100) NOT NULL,
    gate_type       VARCHAR(50)  NOT NULL,  -- phase-approval | security-review | release-sign-off
    status          VARCHAR(20)  DEFAULT 'pending',  -- pending | approved | rejected
    requested_at    TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
    decided_at      TIMESTAMP,
    decided_by      VARCHAR(100),
    reason          TEXT,
    metadata        JSONB        DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_human_gates_task   ON human_gates(task_id);
CREATE INDEX IF NOT EXISTS idx_human_gates_status ON human_gates(status);

-- ─────────────────────────────────────────────
-- V3: MULTI-TENANCY + USAGE TRACKING + INGEST
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS usage_log (
    id          BIGSERIAL    PRIMARY KEY,
    tenant_id   TEXT         NOT NULL,
    project_id  TEXT,
    endpoint    TEXT,
    tier        TEXT,
    model       TEXT,
    tokens_in   INTEGER      DEFAULT 0,
    tokens_out  INTEGER      DEFAULT 0,
    latency_ms  INTEGER      DEFAULT 0,
    cost_usd    NUMERIC(10,6) DEFAULT 0,
    created_at  TIMESTAMPTZ  DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS ingest_pipelines (
    id              TEXT         PRIMARY KEY,
    project_id      TEXT,
    tenant_id       TEXT,
    project_path    TEXT,
    deep            BOOLEAN      DEFAULT FALSE,
    status          TEXT         DEFAULT 'queued',
    requested_at    TIMESTAMPTZ  DEFAULT NOW(),
    started_at      TIMESTAMPTZ,
    completed_at    TIMESTAMPTZ,
    error           TEXT,
    files_indexed   INTEGER      DEFAULT 0,
    nodes_created   INTEGER      DEFAULT 0,
    edges_created   INTEGER      DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_tasks_tenant          ON tasks(tenant_id);
CREATE INDEX IF NOT EXISTS idx_tasks_project_tenant  ON tasks(project_id, tenant_id);
CREATE INDEX IF NOT EXISTS idx_usage_log_tenant      ON usage_log(tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_status         ON ingest_pipelines(status, requested_at DESC);

-- ─────────────────────────────────────────────
-- SEED DATA
-- ─────────────────────────────────────────────

INSERT INTO tenants (id, name, api_key)
VALUES ('sinc-tenant', 'SINC Default Tenant', 'sk-sinc-123456')
ON CONFLICT (id) DO NOTHING;

INSERT INTO projects (id, tenant_id, name)
VALUES ('sinc', 'sinc-tenant', 'SINC AI Infrastructure')
ON CONFLICT (id) DO UPDATE SET tenant_id = EXCLUDED.tenant_id;

INSERT INTO tenants (id, name, api_key, plan)
VALUES ('local', 'Local Development', 'dev', 'enterprise')
ON CONFLICT (id) DO NOTHING;

-- ─────────────────────────────────────────────
-- V4: RATE LIMITS, QUOTAS, WEBHOOKS, API KEYS,
--     IDEMPOTENCY, OUTBOUND WEBHOOKS
-- ─────────────────────────────────────────────

-- Quota / rate-limit columns on tenants
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS requests_per_minute  INTEGER     DEFAULT 60;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tokens_per_day        BIGINT      DEFAULT 500000;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_url           TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_secret        TEXT;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS updated_at            TIMESTAMPTZ DEFAULT NOW();

-- Plan-based defaults
UPDATE tenants SET requests_per_minute = 10,   tokens_per_day = 50000    WHERE plan = 'free'       AND requests_per_minute = 60;
UPDATE tenants SET requests_per_minute = 120,  tokens_per_day = 1000000  WHERE plan = 'pro'        AND requests_per_minute = 60;
UPDATE tenants SET requests_per_minute = 600,  tokens_per_day = 10000000 WHERE plan = 'enterprise' AND requests_per_minute = 60;

-- Named API keys (supports key rotation without changing tenant primary key)
CREATE TABLE IF NOT EXISTS api_keys (
    id          TEXT         PRIMARY KEY,
    tenant_id   TEXT         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    key         TEXT         UNIQUE NOT NULL,
    name        TEXT         DEFAULT 'default',
    created_at  TIMESTAMPTZ  DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    revoked_at  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_api_keys_key       ON api_keys(key) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_api_keys_tenant    ON api_keys(tenant_id);

-- Idempotency keys for POST endpoints
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key             TEXT         PRIMARY KEY,
    tenant_id       TEXT         NOT NULL,
    endpoint        TEXT         NOT NULL,
    status_code     INTEGER      DEFAULT 200,
    response_body   TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_idempotency_tenant ON idempotency_keys(tenant_id, created_at DESC);

-- Outbound webhook deliveries (per tenant, not just per agent)
CREATE TABLE IF NOT EXISTS outbound_webhooks (
    id              BIGSERIAL    PRIMARY KEY,
    tenant_id       TEXT         NOT NULL,
    event_type      TEXT         NOT NULL,  -- task.completed | ingest.done | gate.requested | ask.done
    payload         JSONB        NOT NULL DEFAULT '{}',
    target_url      TEXT         NOT NULL,
    status          TEXT         DEFAULT 'pending',  -- pending | delivered | failed | skipped
    attempts        INTEGER      DEFAULT 0,
    next_attempt_at TIMESTAMPTZ  DEFAULT NOW(),
    delivered_at    TIMESTAMPTZ,
    last_error      TEXT,
    created_at      TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outbound_webhooks_pending
    ON outbound_webhooks(status, next_attempt_at)
    WHERE status IN ('pending', 'failed');
CREATE INDEX IF NOT EXISTS idx_outbound_webhooks_tenant
    ON outbound_webhooks(tenant_id, created_at DESC);

-- Add stats columns missing from ingest_pipelines
ALTER TABLE ingest_pipelines ADD COLUMN IF NOT EXISTS progress   INTEGER DEFAULT 0;
ALTER TABLE ingest_pipelines ADD COLUMN IF NOT EXISTS stats      JSONB   DEFAULT '{}';
ALTER TABLE ingest_pipelines ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE ingest_pipelines ADD COLUMN IF NOT EXISTS finished_at TIMESTAMPTZ;

-- Index for faster usage quota checks
CREATE INDEX IF NOT EXISTS idx_usage_log_tenant_day
    ON usage_log(tenant_id, created_at)
    WHERE created_at > NOW() - INTERVAL '1 day';

-- Seed api_keys from existing tenants (so old api_key column keeps working)
INSERT INTO api_keys (id, tenant_id, key, name)
SELECT 'ak-' || id, id, api_key, 'primary'
FROM tenants
ON CONFLICT (key) DO NOTHING;

-- ─────────────────────────────────────────────
-- V5: CLIENT PUSH STATE
-- Clients push loop state + events via API instead of shared filesystem.
-- ─────────────────────────────────────────────

-- Per-tenant loop state pushed by Invoke-AutonomousLoopV2.ps1
CREATE TABLE IF NOT EXISTS loop_states (
    id           BIGSERIAL    PRIMARY KEY,
    tenant_id    TEXT         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    project_id   TEXT         NOT NULL DEFAULT '',
    cycle        INTEGER      DEFAULT 0,
    phase        TEXT,
    status       TEXT         DEFAULT 'running',
    summary      TEXT,
    metadata     JSONB        DEFAULT '{}',
    updated_at   TIMESTAMPTZ  DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_loop_states_tenant_project
    ON loop_states(tenant_id, project_id);

-- Policy reports pushed by Invoke-PolicyEnforcer.ps1
CREATE TABLE IF NOT EXISTS policy_reports (
    id           BIGSERIAL    PRIMARY KEY,
    tenant_id    TEXT         NOT NULL,
    project_id   TEXT         NOT NULL DEFAULT '',
    report       JSONB        NOT NULL DEFAULT '{}',
    violations   INTEGER      DEFAULT 0,
    status       TEXT         DEFAULT 'ok',
    created_at   TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_policy_reports_tenant
    ON policy_reports(tenant_id, created_at DESC);

-- ─────────────────────────────────────────────
-- V6: AUDIT LOG, TENANT METADATA, SIGNUP
-- ─────────────────────────────────────────────

-- Immutable audit trail (never DELETE from this table)
CREATE TABLE IF NOT EXISTS audit_log (
    id           BIGSERIAL    PRIMARY KEY,
    action       TEXT         NOT NULL,          -- e.g. tenant_created, key_revoked, gdpr_erasure
    actor        TEXT         NOT NULL DEFAULT 'system',
    target_type  TEXT         NOT NULL DEFAULT '',  -- tenant | task | gate | key
    target_id    TEXT         NOT NULL DEFAULT '',
    metadata     JSONB        DEFAULT '{}',
    created_at   TIMESTAMPTZ  DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_created ON audit_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_target  ON audit_log(target_type, target_id);

-- Extra columns on tenants for signup/features
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS updated_at     TIMESTAMPTZ DEFAULT NOW();
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS requests_per_minute INTEGER DEFAULT 60;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tokens_per_day  INTEGER  DEFAULT 500000;
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_url     TEXT     DEFAULT '';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS webhook_secret  TEXT     DEFAULT '';
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS metadata        JSONB    DEFAULT '{}';
