# Migration Guardrails

## Purpose

Evitar que a migração remova capacidades novas ou desligue o legado antes da hora.

## Guardrails

1. Não apagar nem simplificar APIs já existentes em Python para acomodar o legado.
2. Não mover a fonte canônica de volta para `task-dag.json`.
3. Não aceitar fallback silencioso em startup para módulos críticos.
4. Não manter métricas mockadas no dashboard após a fase P0.
5. Não misturar worker runtime e API server no mesmo processo principal.
6. Não introduzir uma terceira árvore de runtime.
7. Não desligar `ai-orchestrator/scripts/v2` enquanto houver qualquer capability `high risk` sem paridade.
8. Não reabrir dependência estrutural em MySQL para estado do orquestrador.
9. Não migrar "script por script" quando a capacidade exige redesenho assíncrono.
10. Não registrar task de migração no DAG legado antes da estabilização do state plane.

## Regression Budget

Durante a migração, as seguintes superfícies têm tolerância zero a regressão:

- `/tasks`
- `/agents`
- `/events`
- `/health`
- `/dashboard`
- `/ask`
- `/simulate`
- `/twin`
- `/plans`
- `/cognitive`

## Definition of Done for PowerShell Sunset

O legado só sai quando:

1. `Observer`, `Scheduler`, `Readiness` e `Repair` estiverem operando em Python.
2. `Mutation`, `FinOps` e `DeployVerify` estiverem em produção na nova frente.
3. `task-dag.json` for projeção read-only.
4. o dashboard estiver ligado a métricas reais.
5. houver soak test de 14 dias sem reativar o loop PowerShell.
