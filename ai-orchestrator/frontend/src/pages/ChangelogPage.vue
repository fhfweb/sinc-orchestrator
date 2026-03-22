<template>
  <div class="changelog-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">System Changelog</h1>
        <p class="text-muted">History of deployments, configuration changes, and schema migrations</p>
      </div>
      <div class="flex gap-2">
        <select class="filter-select" v-model="filterType">
          <option value="">All Types</option>
          <option value="deploy">Deploy</option>
          <option value="config">Config</option>
          <option value="schema">Schema</option>
          <option value="hotfix">Hotfix</option>
          <option value="rollback">Rollback</option>
        </select>
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? '...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <div class="timeline">
      <div
        v-for="entry in filteredEntries"
        :key="entry.id"
        class="timeline-entry"
      >
        <div class="timeline-dot" :class="'dot-' + entry.type"></div>
        <div class="timeline-line"></div>
        <div class="card timeline-card">
          <div class="entry-header flex items-center justify-between">
            <div class="flex items-center gap-2">
              <span class="badge" :class="typeBadge(entry.type)">{{ entry.type }}</span>
              <span class="entry-title">{{ entry.title }}</span>
            </div>
            <div class="entry-meta flex items-center gap-2">
              <span class="text-muted" style="font-size:.75rem;">by {{ entry.author }}</span>
              <span class="mono text-muted" style="font-size:.72rem;">{{ entry.timestamp }}</span>
            </div>
          </div>
          <div class="entry-services flex gap-2" style="margin-top:.5rem; flex-wrap:wrap;">
            <span
              v-for="svc in entry.services"
              :key="svc"
              class="badge badge-info"
              style="font-size:.7rem;"
            >{{ svc }}</span>
          </div>
          <div v-if="entry.description" class="entry-desc text-muted">{{ entry.description }}</div>
        </div>
      </div>
      <div v-if="!filteredEntries.length" class="text-muted" style="padding:2rem; text-align:center;">
        No changelog entries match the filter
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const store = useAppStore()

interface ChangelogEntry {
  id: string
  timestamp: string
  type: 'deploy' | 'config' | 'schema' | 'hotfix' | 'rollback'
  title: string
  author: string
  services: string[]
  description?: string
}

const loading = ref(false)
const filterType = ref('')
const entries = ref<ChangelogEntry[]>([])

const demoEntries: ChangelogEntry[] = [
  {
    id: 'cl-001', timestamp: '2026-03-22 14:00 UTC', type: 'deploy',
    title: 'Deploy v2.5.0-rc1 to Green environment',
    author: 'CI/CD Bot', services: ['API Gateway', 'LLM Router', 'RAG Engine'],
    description: 'Release candidate includes blue-green switch support and improved entropy tracking.'
  },
  {
    id: 'cl-002', timestamp: '2026-03-22 11:30 UTC', type: 'config',
    title: 'Updated LLM fallback order — Groq before Mistral',
    author: 'Fernando G.', services: ['LLM Router'],
    description: 'Groq latency consistently lower than Mistral in p99. Adjusted priority queue.'
  },
  {
    id: 'cl-003', timestamp: '2026-03-22 09:15 UTC', type: 'schema',
    title: 'Migration 0042: add entropy_score column to agent_runs',
    author: 'DB Migration Bot', services: ['PostgreSQL'],
    description: 'Added entropy_score FLOAT column to agent_runs table for analytics.'
  },
  {
    id: 'cl-004', timestamp: '2026-03-21 18:45 UTC', type: 'hotfix',
    title: 'Fix token budget not resetting at midnight UTC',
    author: 'Fernando G.', services: ['Token Budget Service'],
    description: 'Scheduler cron was in local timezone instead of UTC. Fixed and redeployed.'
  },
  {
    id: 'cl-005', timestamp: '2026-03-21 14:00 UTC', type: 'deploy',
    title: 'Deploy v2.4.1 to Blue environment',
    author: 'CI/CD Bot', services: ['API Gateway', 'Auth Service', 'Memory Service'],
    description: 'Includes performance improvements to the memory sync pipeline.'
  },
  {
    id: 'cl-006', timestamp: '2026-03-21 10:20 UTC', type: 'config',
    title: 'Increase Qdrant collection shard count from 2 to 4',
    author: 'Ops Team', services: ['Qdrant'],
    description: 'Vector search latency was increasing at >90K vectors. Resharded to 4.'
  },
  {
    id: 'cl-007', timestamp: '2026-03-20 16:00 UTC', type: 'rollback',
    title: 'Rolled back v2.4.0 — Redis connection pool exhaustion',
    author: 'On-call: Maria S.', services: ['Redis', 'API Gateway'],
    description: 'v2.4.0 had a connection leak in the session middleware. Rolled back to v2.3.9.'
  },
  {
    id: 'cl-008', timestamp: '2026-03-20 09:00 UTC', type: 'deploy',
    title: 'Deploy v2.4.0 to Blue environment',
    author: 'CI/CD Bot', services: ['All Services'],
    description: 'Major release: multi-tenant memory isolation, SSE streaming improvements.'
  },
  {
    id: 'cl-009', timestamp: '2026-03-19 15:30 UTC', type: 'schema',
    title: 'Migration 0041: create compliance_controls table',
    author: 'DB Migration Bot', services: ['PostgreSQL'],
    description: 'New table for tracking compliance control status per tenant.'
  },
  {
    id: 'cl-010', timestamp: '2026-03-18 12:00 UTC', type: 'config',
    title: 'Set GDPR data retention to 90 days for session logs',
    author: 'Legal / Compliance', services: ['PostgreSQL', 'Archive Service'],
  },
]

const filteredEntries = computed(() => {
  if (!filterType.value) return entries.value
  return entries.value.filter(e => e.type === filterType.value)
})

function typeBadge(type: string) {
  const map: Record<string, string> = {
    deploy: 'badge-ok',
    config: 'badge-info',
    schema: 'badge-warn',
    hotfix: 'badge-err',
    rollback: 'badge-err',
  }
  return map[type] ?? 'badge-info'
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/system/changelog')
    entries.value = res.entries ?? res
  } catch {
    entries.value = demoEntries
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.changelog-page { padding: 1.5rem; max-width: 1000px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.filter-select {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .35rem .6rem;
  font-size: .85rem;
  outline: none;
}

.timeline { display: flex; flex-direction: column; gap: 0; padding-left: 2rem; position: relative; }

.timeline-entry {
  display: flex;
  gap: 1rem;
  position: relative;
  padding-bottom: 1rem;
}
.timeline-entry:last-child .timeline-line { display: none; }

.timeline-dot {
  position: absolute;
  left: -2rem;
  top: 1rem;
  width: 12px; height: 12px;
  border-radius: 50%;
  flex-shrink: 0;
  border: 2px solid var(--bg2);
  z-index: 1;
}
.timeline-line {
  position: absolute;
  left: -1.55rem;
  top: 1.75rem;
  bottom: 0;
  width: 2px;
  background: var(--panel-border);
}

.dot-deploy { background: var(--accent3); box-shadow: 0 0 6px var(--accent3); }
.dot-config { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.dot-schema { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
.dot-hotfix { background: var(--danger); box-shadow: 0 0 6px var(--danger); }
.dot-rollback { background: var(--danger); box-shadow: 0 0 6px var(--danger); }

.timeline-card { flex: 1; padding: .9rem 1rem; }
.entry-header { flex-wrap: wrap; gap: .4rem; }
.entry-title { font-weight: 600; font-size: .92rem; }
.entry-meta { flex-shrink: 0; }
.entry-services { }
.entry-desc { font-size: .82rem; margin-top: .4rem; line-height: 1.5; }
</style>
