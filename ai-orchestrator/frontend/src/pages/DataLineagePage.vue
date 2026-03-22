<template>
  <div class="lineage-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Data Lineage</h1>
        <p class="text-muted">Visualize data flow from sources through transforms to sinks</p>
      </div>
      <div class="flex gap-2">
        <input class="search-input" v-model="search" placeholder="Highlight node..." />
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? '...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <div class="lineage-container">
      <!-- Sources Column -->
      <div class="lineage-column">
        <div class="column-header">
          <span class="column-title">Sources</span>
          <span class="badge badge-info">{{ lineage.sources.length }}</span>
        </div>
        <div class="nodes-list">
          <div
            v-for="node in lineage.sources"
            :key="node.id"
            class="node-card card"
            :class="{ highlighted: isHighlighted(node.name), 'node-ok': node.status === 'ok', 'node-warn': node.status === 'warn', 'node-err': node.status === 'err' }"
          >
            <div class="node-name">{{ node.name }}</div>
            <div class="node-type text-muted">{{ node.type }}</div>
            <div class="node-stats flex items-center justify-between">
              <span class="mono" style="font-size:.75rem;">{{ node.records.toLocaleString() }} records</span>
              <span class="status-dot" :class="'sdot-' + node.status"></span>
            </div>
            <div class="text-muted" style="font-size:.7rem;">Updated {{ node.lastUpdated }}</div>
          </div>
        </div>
      </div>

      <!-- Arrow -->
      <div class="flow-arrow-col" aria-hidden="true">
        <div class="flow-arrow">→</div>
      </div>

      <!-- Transforms Column -->
      <div class="lineage-column">
        <div class="column-header">
          <span class="column-title">Transforms</span>
          <span class="badge badge-info">{{ lineage.transforms.length }}</span>
        </div>
        <div class="nodes-list">
          <div
            v-for="node in lineage.transforms"
            :key="node.id"
            class="node-card card node-transform"
            :class="{ highlighted: isHighlighted(node.name) }"
          >
            <div class="node-name">{{ node.name }}</div>
            <div class="node-type text-muted">{{ node.type }}</div>
            <div class="node-stats flex items-center justify-between">
              <span class="mono" style="font-size:.75rem;">{{ node.records.toLocaleString() }} ops/hr</span>
              <span class="status-dot" :class="'sdot-' + node.status"></span>
            </div>
            <div class="text-muted" style="font-size:.7rem;">Updated {{ node.lastUpdated }}</div>
          </div>
        </div>
      </div>

      <!-- Arrow -->
      <div class="flow-arrow-col" aria-hidden="true">
        <div class="flow-arrow">→</div>
      </div>

      <!-- Sinks Column -->
      <div class="lineage-column">
        <div class="column-header">
          <span class="column-title">Sinks</span>
          <span class="badge badge-info">{{ lineage.sinks.length }}</span>
        </div>
        <div class="nodes-list">
          <div
            v-for="node in lineage.sinks"
            :key="node.id"
            class="node-card card node-sink"
            :class="{ highlighted: isHighlighted(node.name) }"
          >
            <div class="node-name">{{ node.name }}</div>
            <div class="node-type text-muted">{{ node.type }}</div>
            <div class="node-stats flex items-center justify-between">
              <span class="mono" style="font-size:.75rem;">{{ node.records.toLocaleString() }} writes/hr</span>
              <span class="status-dot" :class="'sdot-' + node.status"></span>
            </div>
            <div class="text-muted" style="font-size:.7rem;">Updated {{ node.lastUpdated }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Legend -->
    <div class="card legend flex gap-2 items-center" style="margin-top:1rem; padding:.75rem 1rem; flex-wrap:wrap;">
      <span class="text-muted" style="font-size:.8rem;">Legend:</span>
      <div class="flex items-center gap-2"><span class="sdot-ok status-dot"></span><span style="font-size:.8rem;">Healthy</span></div>
      <div class="flex items-center gap-2"><span class="sdot-warn status-dot"></span><span style="font-size:.8rem;">Degraded</span></div>
      <div class="flex items-center gap-2"><span class="sdot-err status-dot"></span><span style="font-size:.8rem;">Offline</span></div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const store = useAppStore()

interface LineageNode {
  id: string
  name: string
  type: string
  records: number
  lastUpdated: string
  status: 'ok' | 'warn' | 'err'
}
interface LineageData {
  sources: LineageNode[]
  transforms: LineageNode[]
  sinks: LineageNode[]
}

const loading = ref(false)
const search = ref('')
const lineage = ref<LineageData>({ sources: [], transforms: [], sinks: [] })

const demoLineage: LineageData = {
  sources: [
    { id: 's1', name: 'PostgreSQL', type: 'Relational DB', records: 2840000, lastUpdated: '1 min ago', status: 'ok' },
    { id: 's2', name: 'Redis', type: 'Cache / PubSub', records: 142000, lastUpdated: '5 sec ago', status: 'ok' },
    { id: 's3', name: 'Qdrant', type: 'Vector Store', records: 98500, lastUpdated: '2 min ago', status: 'ok' },
    { id: 's4', name: 'External APIs', type: 'REST / Webhooks', records: 18200, lastUpdated: '8 min ago', status: 'warn' },
  ],
  transforms: [
    { id: 't1', name: 'RAG Engine', type: 'Retrieval Transform', records: 34500, lastUpdated: '30 sec ago', status: 'ok' },
    { id: 't2', name: 'LLM Router', type: 'AI Gateway', records: 12300, lastUpdated: '10 sec ago', status: 'ok' },
    { id: 't3', name: 'Memory Sync', type: 'State Sync', records: 7800, lastUpdated: '1 min ago', status: 'ok' },
  ],
  sinks: [
    { id: 'k1', name: 'Dashboard', type: 'WebSocket / SSE', records: 48200, lastUpdated: '5 sec ago', status: 'ok' },
    { id: 'k2', name: 'Reports', type: 'PDF / CSV Export', records: 3100, lastUpdated: '15 min ago', status: 'ok' },
    { id: 'k3', name: 'Webhooks', type: 'HTTP Callbacks', records: 9400, lastUpdated: '3 min ago', status: 'warn' },
  ]
}

function isHighlighted(name: string) {
  if (!search.value) return false
  return name.toLowerCase().includes(search.value.toLowerCase())
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/data-lineage')
    lineage.value = {
      sources: Array.isArray(res.sources) ? res.sources : demoLineage.sources,
      transforms: Array.isArray(res.transforms) ? res.transforms : demoLineage.transforms,
      sinks: Array.isArray(res.sinks) ? res.sinks : demoLineage.sinks,
    }
  } catch {
    lineage.value = demoLineage
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.lineage-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.search-input {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .35rem .6rem;
  font-size: .85rem;
  outline: none;
  transition: var(--transition);
  width: 200px;
}
.search-input:focus { border-color: var(--accent); }

.lineage-container {
  display: flex;
  gap: 0;
  align-items: flex-start;
  overflow-x: auto;
}
.lineage-column {
  flex: 1;
  min-width: 220px;
  display: flex;
  flex-direction: column;
  gap: .5rem;
}
.column-header {
  display: flex;
  align-items: center;
  gap: .5rem;
  margin-bottom: .5rem;
  padding: .5rem .75rem;
  background: var(--bg3);
  border-radius: var(--radius-sm);
}
.column-title { font-weight: 700; font-size: .85rem; }

.flow-arrow-col {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 2rem .75rem 0;
  color: var(--accent);
  font-size: 1.5rem;
  flex-shrink: 0;
}

.nodes-list { display: flex; flex-direction: column; gap: .5rem; }

.node-card {
  padding: .75rem 1rem;
  transition: var(--transition);
  border-left: 3px solid var(--panel-border);
}
.node-card:hover { border-left-color: var(--accent); }
.node-transform { border-left-color: var(--accent2); }
.node-sink { border-left-color: var(--accent3); }
.highlighted {
  border-color: var(--accent) !important;
  background: color-mix(in srgb, var(--accent) 12%, var(--panel)) !important;
  box-shadow: 0 0 12px color-mix(in srgb, var(--accent) 30%, transparent);
}

.node-name { font-weight: 700; font-size: .9rem; margin-bottom: .15rem; }
.node-type { font-size: .75rem; margin-bottom: .4rem; }
.node-stats { margin-bottom: .2rem; }

.status-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; display: inline-block; }
.sdot-ok { background: var(--accent3); box-shadow: 0 0 5px var(--accent3); }
.sdot-warn { background: var(--warn); box-shadow: 0 0 5px var(--warn); }
.sdot-err { background: var(--danger); box-shadow: 0 0 5px var(--danger); }

.legend { gap: 1rem; }
</style>
