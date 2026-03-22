import { createRouter, createWebHistory } from 'vue-router'
import type { RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', redirect: '/noc' },

  // ── Pinned / NOC essentials ────────────────────────────────────
  { path: '/noc',         component: () => import('@/pages/Home.vue'),         meta: { label: 'Visão Geral',  icon: '⬡' } },
  { path: '/noc/logs',    component: () => import('@/pages/LogsPage.vue'),     meta: { label: 'Live Logs',    icon: '▶' } },
  { path: '/noc/kanban',  component: () => import('@/pages/KanbanPage.vue'),   meta: { label: 'Job Board',    icon: '⊞' } },
  { path: '/noc/agents',  component: () => import('@/pages/AgentsPage.vue'),   meta: { label: 'Agentes',      icon: '◈' } },
  { path: '/noc/ask',     component: () => import('@/pages/AskPage.vue'),      meta: { label: 'Ask N5',       icon: '✦' } },
  { path: '/noc/metrics', component: () => import('@/pages/MetricsPage.vue'),  meta: { label: 'Métricas RED', icon: '◉' } },

  // ── Execução ───────────────────────────────────────────────────
  { path: '/noc/tasks',          component: () => import('@/pages/TasksPage.vue'),        meta: { label: 'Tarefas',      group: 'exec' } },
  { path: '/noc/tasks/batch',    component: () => import('@/pages/TasksPage.vue'),        meta: { label: 'Batch Ops',    group: 'exec' } },
  { path: '/noc/task-templates', component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Templates',    group: 'exec' } },
  { path: '/noc/rollback',       component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Rollback',     group: 'exec' } },
  { path: '/noc/engine',         component: () => import('@/pages/EnginePage.vue'),       meta: { label: 'Engine Room',  group: 'exec' } },
  { path: '/noc/plans',          component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Planos',       group: 'exec' } },
  { path: '/noc/incidents',      component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Incidents',    group: 'exec' } },

  // ── Inteligência ───────────────────────────────────────────────
  { path: '/noc/cognitive',      component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Cognitive',      group: 'intel' } },
  { path: '/noc/mcts',           component: () => import('@/pages/StubPage.vue'),         meta: { label: 'MCTS',           group: 'intel' } },
  { path: '/noc/concept-drift',  component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Concept Drift',  group: 'intel' } },
  { path: '/noc/ab-test',        component: () => import('@/pages/StubPage.vue'),         meta: { label: 'A/B Test',       group: 'intel' } },
  { path: '/noc/learn-velocity', component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Learn Velocity', group: 'intel' } },
  { path: '/noc/anomalies',      component: () => import('@/pages/AnomaliesPage.vue'),    meta: { label: 'Anomalias',      group: 'intel' } },
  { path: '/noc/correlations',   component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Correlações',    group: 'intel' } },
  { path: '/noc/simulate',       component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Simulação',      group: 'intel' } },

  // ── LLM Ops ────────────────────────────────────────────────────
  { path: '/noc/llm',            component: () => import('@/pages/LlmPage.vue'),          meta: { label: 'LLM Status',     group: 'llm' } },
  { path: '/noc/token-budgets',  component: () => import('@/pages/TokenBudgetsPage.vue'), meta: { label: 'Token Budgets',  group: 'llm' } },
  { path: '/noc/context-traces', component: () => import('@/pages/ContextTracesPage.vue'),meta: { label: 'Context Traces', group: 'llm' } },
  { path: '/noc/entropy',        component: () => import('@/pages/EntropyPage.vue'),      meta: { label: 'Entropy',        group: 'llm' } },
  { path: '/noc/playground',     component: () => import('@/pages/PlaygroundPage.vue'),   meta: { label: 'Playground',     group: 'llm' } },

  // ── Infraestrutura ─────────────────────────────────────────────
  { path: '/noc/redis',          component: () => import('@/pages/RedisPage.vue'),        meta: { label: 'Redis',       group: 'infra' } },
  { path: '/noc/db',             component: () => import('@/pages/DbPage.vue'),           meta: { label: 'DB Console',  group: 'infra' } },
  { path: '/noc/migrations',     component: () => import('@/pages/MigrationsPage.vue'),   meta: { label: 'Migrações',   group: 'infra' } },
  { path: '/noc/queue',          component: () => import('@/pages/StubPage.vue'),         meta: { label: 'Queue Heat',  group: 'infra' } },
  { path: '/noc/blue-green',     component: () => import('@/pages/BlueGreenPage.vue'),    meta: { label: 'Blue/Green',  group: 'infra' } },
  { path: '/noc/canary',         component: () => import('@/pages/CanaryPage.vue'),       meta: { label: 'Canary',      group: 'infra' } },
  { path: '/noc/capacity',       component: () => import('@/pages/CapacityPage.vue'),     meta: { label: 'Capacity',    group: 'infra' } },

  // ── Observabilidade ────────────────────────────────────────────
  { path: '/noc/tracing',        component: () => import('@/pages/TracingPage.vue'),      meta: { label: 'Tracing',     group: 'obs' } },
  { path: '/noc/system',         component: () => import('@/pages/StubPage.vue'),         meta: { label: 'System',      group: 'obs' } },
  { path: '/noc/health',         component: () => import('@/pages/HealthPage.vue'),       meta: { label: 'Health Grid', group: 'obs' } },
  { path: '/noc/usage',          component: () => import('@/pages/UsagePage.vue'),        meta: { label: 'Usage',       group: 'obs' } },
  { path: '/noc/changelog',      component: () => import('@/pages/ChangelogPage.vue'),    meta: { label: 'Changelog',   group: 'obs' } },

  // ── Conhecimento ───────────────────────────────────────────────
  { path: '/noc/knowledge',      component: () => import('@/pages/KnowledgePage.vue'),    meta: { label: 'Knowledge',    group: 'know' } },
  { path: '/noc/memory',         component: () => import('@/pages/MemoryPage.vue'),       meta: { label: 'Memory',       group: 'know' } },
  { path: '/noc/data-lineage',   component: () => import('@/pages/DataLineagePage.vue'),  meta: { label: 'Data Lineage', group: 'know' } },
  { path: '/noc/runbooks',       component: () => import('@/pages/RunbooksPage.vue'),     meta: { label: 'Runbooks',     group: 'know' } },
  { path: '/noc/postmortems',    component: () => import('@/pages/PostmortemsPage.vue'),  meta: { label: 'Postmortems',  group: 'know' } },

  // ── Segurança ──────────────────────────────────────────────────
  { path: '/noc/rbac',             component: () => import('@/pages/RbacPage.vue'),          meta: { label: 'RBAC',           group: 'sec' } },
  { path: '/noc/secrets',          component: () => import('@/pages/SecretsPage.vue'),        meta: { label: 'Secrets',        group: 'sec' } },
  { path: '/noc/compliance',       component: () => import('@/pages/CompliancePage.vue'),     meta: { label: 'Compliance',     group: 'sec' } },
  { path: '/noc/tenant-isolation', component: () => import('@/pages/StubPage.vue'),           meta: { label: 'Isolation Scan', group: 'sec' } },
  { path: '/noc/chaos',            component: () => import('@/pages/ChaosPage.vue'),          meta: { label: 'Chaos Eng',      group: 'sec' } },

  // ── Multi-tenancy ──────────────────────────────────────────────
  { path: '/noc/tenants',          component: () => import('@/pages/TenantsPage.vue'),        meta: { label: 'Tenants',       group: 'multi' } },
  { path: '/noc/onboarding',       component: () => import('@/pages/StubPage.vue'),           meta: { label: 'Onboarding',    group: 'multi' } },
  { path: '/noc/feature-flags',    component: () => import('@/pages/FeatureFlagsPage.vue'),   meta: { label: 'Feature Flags', group: 'multi' } },

  // ── FinOps ─────────────────────────────────────────────────────
  { path: '/noc/billing',          component: () => import('@/pages/BillingPage.vue'),        meta: { label: 'Billing',   group: 'finops' } },
  { path: '/noc/costs',            component: () => import('@/pages/StubPage.vue'),           meta: { label: 'Costs',     group: 'finops' } },
  { path: '/noc/quotas',           component: () => import('@/pages/StubPage.vue'),           meta: { label: 'Quotas',    group: 'finops' } },
  { path: '/noc/tenant-analytics', component: () => import('@/pages/StubPage.vue'),           meta: { label: 'Analytics', group: 'finops' } },

  // ── Operações ──────────────────────────────────────────────────
  { path: '/noc/connect', component: () => import('@/pages/ConnectPage.vue'), meta: { label: 'Connect', group: 'ops' } },
  { path: '/noc/sdk',     component: () => import('@/pages/SdkPage.vue'),     meta: { label: 'SDK Gen', group: 'ops' } },

  // ── Developer ──────────────────────────────────────────────────
  { path: '/noc/twin',  component: () => import('@/pages/TwinPage.vue'),  meta: { label: 'Digital Twin', group: 'dev' } },
  { path: '/noc/gates', component: () => import('@/pages/GatesPage.vue'), meta: { label: 'Gates',        group: 'dev' } },

  { path: '/:pathMatch(.*)*', redirect: '/noc' }
]

export const router = createRouter({
  history: createWebHistory(),
  routes
})
