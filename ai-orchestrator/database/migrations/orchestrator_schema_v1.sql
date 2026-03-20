-- SINC Orchestrator Schema V1
-- Target: PostgreSQL 17

DROP TABLE IF EXISTS dependencies;
DROP TABLE IF EXISTS tasks;
DROP TABLE IF EXISTS projects;

CREATE TABLE projects (
    id VARCHAR(50) PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tasks (
    id VARCHAR(100) PRIMARY KEY,
    project_id VARCHAR(50) REFERENCES projects(id),
    status VARCHAR(50) DEFAULT 'pending',
    assigned_agent VARCHAR(100),
    description TEXT,
    priority VARCHAR(10) DEFAULT 'P2',
    lock_ttl INTEGER DEFAULT 20,
    critical_path BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    started_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    metadata JSONB DEFAULT '{}'
);

CREATE TABLE dependencies (
    task_id VARCHAR(100) REFERENCES tasks(id) ON DELETE CASCADE,
    dependency_id VARCHAR(100) REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (task_id, dependency_id)
);

-- Seed Initial Project
INSERT INTO projects (id, name) VALUES ('sinc', 'SINC AI Infrastructure') ON CONFLICT (id) DO NOTHING;
