# ADR-0004: Python Control Plane as Canonical Runtime

- Date: 2026-03-19
- Status: Accepted

## Context

O orquestrador evoluiu para duas frentes de execução:

- `scripts/v2`, responsável pelo loop autônomo legado e pela maior parte da operação madura
- `ai-orchestrator/services`, responsável pela API moderna, streaming, watchdog, cognitive services, analytics e digital twin

Manter ambos como candidatos a runtime principal cria problemas estruturais:

- drift entre árvores de código
- duplicação de responsabilidades
- acoplamento entre DB, DAG e arquivos de projeção
- aumento do custo de manutenção
- dificuldade para estabelecer uma fonte canônica de estado

Ao mesmo tempo, desligar o legado imediatamente é arriscado porque capacidades operacionais importantes ainda não têm paridade em Python.

## Decision

- `Python` passa a ser a plataforma-alvo e o control plane canônico do orquestrador.
- `PowerShell` entra em sunset controlado.
- `scripts/v2` permanece ativo apenas até a conclusão da matriz de paridade publicada em `documentation/migration/runtime-capability-matrix.md`.
- Nenhuma funcionalidade nova deve ser adicionada ao runtime PowerShell; apenas bugfixes e compatibilidade são permitidos.
- `Postgres` será a fonte canônica do estado operacional.
- `task-dag.json` será reduzido a projeção read-only após a consolidação do state plane.
- O desligamento do legado só poderá ocorrer depois de:
  - observer/scheduler/readiness/repair em Python
  - finops/mutation/deploy/policy/release em Python
  - soak test mínimo de 14 dias

## Consequences

### Positive

- unifica a direção técnica do produto
- reduz o risco de terceira arquitetura emergir
- preserva a frente moderna já construída
- facilita observabilidade, async IO, workers e integração com Postgres/Redis

### Negative

- exige um programa de migração explícito e disciplinado
- prolonga temporariamente a coexistência do legado
- obriga a manter paridade funcional antes do corte final

### Constraints

- não é permitido simplificar a frente Python para encaixá-la no modelo legado
- não é permitido desligar PowerShell por conveniência antes da paridade P0/P1
- não é permitido manter dual-write canônico entre DB e DAG após a fase de consolidação
