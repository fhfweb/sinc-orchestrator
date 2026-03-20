-- Roteamento de caminho (auditoria + calibração)
CREATE TABLE IF NOT EXISTS execution_routes (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    path        TEXT NOT NULL,   -- instant | fast | standard | deep
    reason      TEXT,
    decided_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_routes_tenant_path ON execution_routes(tenant_id, path, decided_at DESC);

-- Adaptações de goal (rastreabilidade)
CREATE TABLE IF NOT EXISTS goal_adaptations (
    id               BIGSERIAL PRIMARY KEY,
    goal_id          TEXT NOT NULL,
    adaptation_type  TEXT NOT NULL,   -- reorder | cancel_subtask | add_subtask | escalate
    affected_tasks   JSONB,
    reason           TEXT,
    created_at       TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_goal_adaptations_goal ON goal_adaptations(goal_id, created_at DESC);

-- Histórico de confiança em runtime
CREATE TABLE IF NOT EXISTS runtime_confidence_events (
    id          BIGSERIAL PRIMARY KEY,
    task_id     TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    from_level  TEXT,
    to_level    TEXT,
    trigger     TEXT,
    action_taken TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_rce_task ON runtime_confidence_events(task_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_rce_tenant ON runtime_confidence_events(tenant_id, created_at DESC);

-- Resultados de estratégias exploratórias (para calibrar o gerador)
CREATE TABLE IF NOT EXISTS experimental_strategy_results (
    id                  BIGSERIAL PRIMARY KEY,
    task_id             TEXT NOT NULL,
    tenant_id           TEXT NOT NULL,
    strategy_name       TEXT,
    agent_used          TEXT,
    predicted_success   FLOAT,
    actual_success      BOOLEAN,
    was_selected        BOOLEAN,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_exp_results_tenant ON experimental_strategy_results(tenant_id, created_at DESC);
