-- SINC Orchestrator Schema — Migration 11
-- Cognitive Runtime: Dynamic Rules + Heartbeats
--
-- What this migration adds:
--   1. dynamic_rules      — auto-learned routing rules from agent_events patterns
--   2. heartbeats         — agent liveness tracking for watchdog stale recovery
--   3. task_file_links    — links tasks to files for twin impact analysis
-- ─────────────────────────────────────────────────────────────────────────────


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

COMMENT ON TABLE dynamic_rules IS
    'Auto-learned routing rules mined from agent_events success/failure patterns. '
    'Populated by DynamicRuleEngine.learn_rules_from_history() running every 5 min.';

COMMENT ON COLUMN dynamic_rules.condition IS
    'JSON match criteria, e.g. {"task_type": "fix_bug", "error_signature": "NullPointerException"}';

COMMENT ON COLUMN dynamic_rules.action IS
    'Routing action: "route_to:<agent>", "prefer_agent:<agent>", or "skip_l2"';

COMMENT ON COLUMN dynamic_rules.confidence IS
    'Fraction 0.0–1.0; rules below 0.75 are not applied at runtime';

COMMENT ON COLUMN dynamic_rules.created_from IS
    'How the rule was discovered: "failure_pattern" | "success_pattern" | "manual"';

CREATE INDEX IF NOT EXISTS idx_dynamic_rules_tenant
    ON dynamic_rules (tenant_id);

CREATE INDEX IF NOT EXISTS idx_dynamic_rules_confidence
    ON dynamic_rules (confidence DESC);

CREATE INDEX IF NOT EXISTS idx_dynamic_rules_condition
    ON dynamic_rules USING gin (condition);


-- ─────────────────────────────────────────────
-- 2. HEARTBEATS
-- ─────────────────────────────────────────────
-- Used by the watchdog to detect stale in-progress tasks.
-- Agents must upsert a row here periodically (e.g. every 30s).

CREATE TABLE IF NOT EXISTS heartbeats (
    task_id     UUID         NOT NULL,
    agent_name  TEXT         NOT NULL,
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    PRIMARY KEY (task_id)
);

COMMENT ON TABLE heartbeats IS
    'Agent liveness signal per task. '
    'Watchdog marks tasks stale if no heartbeat within TASK_STALE_TIMEOUT_M minutes.';

CREATE INDEX IF NOT EXISTS idx_heartbeats_updated_at
    ON heartbeats (updated_at);


-- ─────────────────────────────────────────────
-- 3. TASK FILE LINKS
-- ─────────────────────────────────────────────
-- Tracks which files each task touched — used by twin impact analysis.

CREATE TABLE IF NOT EXISTS task_file_links (
    id          BIGSERIAL    PRIMARY KEY,
    task_id     UUID         NOT NULL,
    file_path   TEXT         NOT NULL,
    link_type   TEXT         NOT NULL DEFAULT 'modified',  -- modified | created | deleted | read
    tenant_id   TEXT,
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE task_file_links IS
    'Tracks files touched per task for digital twin impact analysis (/twin/impact/<path>).';

CREATE INDEX IF NOT EXISTS idx_task_file_links_task
    ON task_file_links (task_id);

CREATE INDEX IF NOT EXISTS idx_task_file_links_file
    ON task_file_links (file_path);

CREATE INDEX IF NOT EXISTS idx_task_file_links_tenant
    ON task_file_links (tenant_id);


-- ─────────────────────────────────────────────
-- 4. AUTO-UPDATE updated_at ON dynamic_rules
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
