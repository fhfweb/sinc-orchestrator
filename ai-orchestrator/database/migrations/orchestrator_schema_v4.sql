-- SINC Orchestrator Schema V4
-- Cognitive Runtime: Dynamic Rules, Heartbeats, Task-File Links
-- Safe migration: all statements are idempotent (IF NOT EXISTS / OR REPLACE)
-- Apply on top of v3 for existing databases.

-- ─────────────────────────────────────────────
-- 1. DYNAMIC RULES
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS dynamic_rules (
    rule_id       TEXT         PRIMARY KEY,
    condition     JSONB        NOT NULL,
    action        TEXT         NOT NULL,
    confidence    FLOAT        NOT NULL DEFAULT 0.0,
    created_from  TEXT         NOT NULL DEFAULT 'event_pattern',
    times_applied INTEGER      NOT NULL DEFAULT 0,
    tenant_id     TEXT,
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_dynamic_rules_tenant
    ON dynamic_rules (tenant_id);

CREATE INDEX IF NOT EXISTS idx_dynamic_rules_confidence
    ON dynamic_rules (confidence DESC);

CREATE INDEX IF NOT EXISTS idx_dynamic_rules_condition
    ON dynamic_rules USING gin (condition);


-- ─────────────────────────────────────────────
-- 2. HEARTBEATS
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS heartbeats (
    task_id     UUID         NOT NULL,
    agent_name  TEXT         NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id)
);

CREATE INDEX IF NOT EXISTS idx_heartbeats_updated_at
    ON heartbeats (updated_at);


-- ─────────────────────────────────────────────
-- 3. TASK FILE LINKS
-- ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS task_file_links (
    id          BIGSERIAL    PRIMARY KEY,
    task_id     UUID         NOT NULL,
    file_path   TEXT         NOT NULL,
    link_type   TEXT         NOT NULL DEFAULT 'modified',
    tenant_id   TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_task_file_links_task
    ON task_file_links (task_id);

CREATE INDEX IF NOT EXISTS idx_task_file_links_file
    ON task_file_links (file_path);

CREATE INDEX IF NOT EXISTS idx_task_file_links_tenant
    ON task_file_links (tenant_id);


-- ─────────────────────────────────────────────
-- 4. AUTO-UPDATE TRIGGER
-- ─────────────────────────────────────────────

CREATE OR REPLACE FUNCTION _set_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_dynamic_rules_updated_at'
    ) THEN
        CREATE TRIGGER trg_dynamic_rules_updated_at
            BEFORE UPDATE ON dynamic_rules
            FOR EACH ROW EXECUTE FUNCTION _set_updated_at();
    END IF;
END;
$$;
