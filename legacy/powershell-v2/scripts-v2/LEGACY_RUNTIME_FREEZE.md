# Legacy Runtime Freeze

## Status

`ai-orchestrator/scripts/v2` está em freeze funcional.

## Rules

1. Nenhuma feature nova deve nascer nesta árvore.
2. Apenas bugfixes, compatibilidade e extração de capacidades para a nova frente Python são permitidos.
3. Toda capacidade nova deve ser implementada em `ai-orchestrator/services`.
4. Toda correção feita aqui deve ter contrapartida planejada na matriz de migração.

## Sunset Target

Esta árvore será arquivada quando:

- observer/scheduler/readiness/repair estiverem em Python
- finops/mutation/deploy/policy/release estiverem em Python
- `task-dag.json` for apenas projeção
- o runtime PowerShell deixar de ser necessário no bootstrap padrão
