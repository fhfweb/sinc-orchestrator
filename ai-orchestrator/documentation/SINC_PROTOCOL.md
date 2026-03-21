# SINC Agentic Protocol (Protocolo de Colaboração IA)

Este documento define como agentes externos (como OpenCode, Cline ou outros LLMs) devem interagir com o **SINC Orchestrator** para realizar tarefas complexas de engenharia de software.

## 1. Descoberta de Capacidades
Antes de agir, o agente deve consultar o que o orquestrador pode fazer.
- **Ferramenta MCP**: `get_orchestrator_capabilities`
- **O que retorna**: Lista de agentes ativos (Core-5), skills carregadas no `agents_config.py` e saúde dos backends (Redis/Qdrant/Neo4j).

## 2. Ciclo de Vida da Tarefa (Task Lifecycle)
O SINC opera de forma assíncrona. O fluxo recomendado é:

1. **Criação**: Use `create_sinc_task` enviando um título e uma descrição detalhada.
2. **Acompanhamento**: Use `get_task_status` para monitorar o campo `status` (`pending`, `in-progress`, `done`, `failed`).
3. **Eventos (SSE)**: Se o cliente suportar WebSockets/SSE, ele pode ouvir o canal `task_status_updated` para atualizações em tempo real.

## 3. Uso da Hierarquia de Memória
O orquestrador mantém 5 camadas de memória (L0-L4). Agentes externos devem focar nas ferramentas MCP:
- **Busca Semântica (`search_agent_memory`)**: Para encontrar trechos de código similares ou decisões passadas gravadas no Qdrant.
- **Consulta ao Grafo (`query_graph`)**: Para entender relações estruturais (quem chama quem) no Neo4j.
- **Análise de Impacto (`impact_analysis`)**: Para prever o risco de uma alteração antes de fazê-la.

## 4. Melhores Práticas para Agentes Externos
- **Contexto de Tenant**: Sempre especifique o `tenant_id` (padrão: `local`) para garantir isolamento.
- **IDs de Projeto**: Use o `project_id` correto se estiver trabalhando em múltiplos repositórios.
- **Traceability**: Cada ação gerada via MCP inclui um `X-Trace-Id` que pode ser visto nos logs canônicos do orquestrador.

---
**Status**: Versão 1.0 (Narrativa 12.0)
