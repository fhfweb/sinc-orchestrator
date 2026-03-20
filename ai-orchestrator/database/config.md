# Database Config

## Engine
- Value: mysql
- Host: db
- Source: /ai-orchestrator/docker/.env.docker.generated

## Isolation
- Each project keeps dedicated DB namespace and service name.
- No shared transactional DB between projects.