# PowerShell Deprecation Plan

## Decision

PowerShell será descontinuado como runtime principal do orquestrador.

Isso não significa desligar hoje. Significa:

- manter o legado vivo apenas enquanto houver capacidades críticas sem paridade
- proibir expansão funcional do legado
- migrar capacidades para Python com critérios objetivos

## Why Deprecate PowerShell

### Limits of the Legacy Runtime

- dificuldade de modularidade e composição
- baixa ergonomia para workers assíncronos
- acoplamento alto com arquivos e caminhos locais
- dificuldade de observabilidade moderna
- custo alto de evolução em comparação com FastAPI/Postgres/Redis

### Why Python Is the Correct Target

- já concentra a frente nova de produto
- melhor para API, async IO, workers, DB pools, Redis streams e observabilidade
- melhor para manter serviços de inteligência, twin, simulation e model routing

## Deprecation Strategy

### Stage A - Freeze

- nenhum recurso novo entra em PowerShell
- apenas bugfixes e compatibilidade

### Stage B - Parity

- migrar capacidades do legado em blocos
- validar E2E e soak por capability

### Stage C - Cutover

- Python vira default runtime
- PowerShell fica desligado por padrão, disponível só para rollback

### Stage D - Archive

- mover o legado para `legacy/`
- remover do bootstrap padrão

## Cutover Gate

O corte só pode acontecer quando:

1. observer Python estiver estável
2. scheduler Python estiver estável
3. readiness Python estiver estável
4. repair flow Python estiver estável
5. mutation/finops/deploy pipelines existirem em Python
6. dashboard estiver ligado a métricas reais
7. Postgres estiver operando como state plane único

## Explicit No-Go Conditions

Não desligar PowerShell se qualquer uma destas condições for verdadeira:

- `task-dag.json` ainda for fonte operacional
- readiness depender do legado
- qualquer `high risk` da matriz ainda for `not-migrated`
- incident/repair loop depender do observer legado
- deploy/mutation/finops só existirem em PowerShell
