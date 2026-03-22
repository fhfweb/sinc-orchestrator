<template>
  <div class="memory-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Memory Manager</h1>
        <p class="text-muted">Manage episodic, semantic, and working memory stores</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-ghost" @click="analyzeMemory" :disabled="analyzing">
          {{ analyzing ? 'Analyzing...' : 'Analyze' }}
        </button>
        <button class="btn btn-danger" @click="pruneMemory" :disabled="pruning">
          {{ pruning ? 'Pruning...' : 'Prune Low Quality' }}
        </button>
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? '...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <!-- Stats bar -->
    <div v-if="stats" class="flex gap-2" style="margin-bottom:1.25rem;">
      <div class="card kpi-card">
        <div class="kpi-label text-muted">TOTAL ENTRIES</div>
        <div class="kpi-value text-accent">{{ stats.total }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">AVG QUALITY</div>
        <div class="kpi-value" :class="qualityClass(stats.avgQuality)">{{ (stats.avgQuality * 100).toFixed(0) }}%</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">LOW QUALITY</div>
        <div class="kpi-value text-danger">{{ stats.lowQuality }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">EXPIRED</div>
        <div class="kpi-value text-warn">{{ stats.expired }}</div>
      </div>
    </div>

    <!-- Tabs -->
    <div class="tabs flex gap-2" style="margin-bottom:1rem;">
      <button
        v-for="tab in ['Episodic', 'Semantic', 'Working']"
        :key="tab"
        class="tab-btn"
        :class="{ active: activeTab === tab }"
        @click="activeTab = tab"
      >
        {{ tab }}
        <span class="badge badge-info" style="margin-left:.35rem;">{{ tabCount(tab) }}</span>
      </button>
    </div>

    <!-- Search -->
    <div class="flex gap-2 items-center" style="margin-bottom:.75rem;">
      <input class="search-input" v-model="search" placeholder="Filter memories..." />
    </div>

    <!-- Table -->
    <div class="card" style="overflow-x:auto;">
      <table class="tbl">
        <thead>
          <tr>
            <th>ID</th>
            <th>Type</th>
            <th>Content Preview</th>
            <th>Quality Score</th>
            <th>Created At</th>
            <th>TTL</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="entry in filteredEntries" :key="entry.id">
            <td class="mono truncate" style="max-width:120px; font-size:.75rem;">{{ entry.id }}</td>
            <td><span class="badge badge-info">{{ entry.type }}</span></td>
            <td class="truncate" style="max-width:300px; font-size:.82rem;">{{ entry.content }}</td>
            <td>
              <div class="quality-bar-wrap">
                <div
                  class="quality-bar-fill"
                  :class="qualityFillClass(entry.qualityScore)"
                  :style="{ width: (entry.qualityScore * 100) + '%' }"
                ></div>
              </div>
              <span class="mono" :class="qualityClass(entry.qualityScore)" style="font-size:.75rem;">
                {{ (entry.qualityScore * 100).toFixed(0) }}%
              </span>
            </td>
            <td class="text-muted" style="font-size:.75rem;">{{ entry.createdAt }}</td>
            <td class="mono" :class="ttlClass(entry.ttl)" style="font-size:.75rem;">{{ entry.ttl }}</td>
          </tr>
          <tr v-if="filteredEntries.length === 0">
            <td colspan="6" class="text-muted" style="text-align:center; padding:1.5rem;">No entries found</td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const store = useAppStore()

interface MemoryEntry {
  id: string
  type: string
  content: string
  qualityScore: number
  createdAt: string
  ttl: string
  tab: 'Episodic' | 'Semantic' | 'Working'
}
interface Stats { total: number; avgQuality: number; lowQuality: number; expired: number }

const loading = ref(false)
const pruning = ref(false)
const analyzing = ref(false)
const activeTab = ref('Episodic')
const search = ref('')
const entries = ref<MemoryEntry[]>([])
const stats = ref<Stats | null>(null)

const demoEntries: MemoryEntry[] = [
  { id: 'mem-a1b2c3d4e5f6', type: 'conversation', content: 'User asked about GDPR compliance requirements for data retention policy in EU regions', qualityScore: 0.92, createdAt: '2026-03-22 14:00', ttl: '7d', tab: 'Episodic' },
  { id: 'mem-b2c3d4e5f6g7', type: 'event', content: 'Deployment event: v2.4.1 deployed to production at 13:45 UTC', qualityScore: 0.88, createdAt: '2026-03-22 13:45', ttl: '30d', tab: 'Episodic' },
  { id: 'mem-c3d4e5f6g7h8', type: 'decision', content: 'Agent decided to use RAG retrieval with threshold 0.75 for this query type', qualityScore: 0.45, createdAt: '2026-03-22 12:30', ttl: '1d', tab: 'Episodic' },
  { id: 'mem-d4e5f6g7h8i9', type: 'fact', content: 'PostgreSQL connection pool max_connections = 100, currently 78 in use', qualityScore: 0.96, createdAt: '2026-03-21 09:00', ttl: '∞', tab: 'Semantic' },
  { id: 'mem-e5f6g7h8i9j0', type: 'concept', content: 'RAG pipeline: query → embed → retrieve (top-k=5) → rerank → inject → generate', qualityScore: 0.91, createdAt: '2026-03-20 16:00', ttl: '∞', tab: 'Semantic' },
  { id: 'mem-f6g7h8i9j0k1', type: 'procedure', content: 'To add a new tenant: create DB schema, seed config, register in auth service', qualityScore: 0.72, createdAt: '2026-03-19 11:00', ttl: '90d', tab: 'Semantic' },
  { id: 'mem-g7h8i9j0k1l2', type: 'context', content: 'Current sprint goal: implement blue-green deployment with zero downtime', qualityScore: 0.85, createdAt: '2026-03-22 14:30', ttl: '2h', tab: 'Working' },
  { id: 'mem-h8i9j0k1l2m3', type: 'draft', content: 'Draft response for compliance report generation — pending review', qualityScore: 0.31, createdAt: '2026-03-22 14:28', ttl: '1h', tab: 'Working' },
  { id: 'mem-i9j0k1l2m3n4', type: 'scratch', content: 'Intermediate calc: token budget remaining = 450000 / 1000000 for tenant-001', qualityScore: 0.22, createdAt: '2026-03-22 14:25', ttl: '30m', tab: 'Working' },
]

const demoStats: Stats = { total: 9, avgQuality: 0.69, lowQuality: 3, expired: 1 }

const filteredEntries = computed(() => {
  return entries.value.filter(e => {
    if (e.tab !== activeTab.value) return false
    if (search.value && !e.content.toLowerCase().includes(search.value.toLowerCase()) && !e.id.includes(search.value)) return false
    return true
  })
})

function tabCount(tab: string) { return entries.value.filter(e => e.tab === tab).length }
function qualityClass(q: number) {
  if (q >= 0.8) return 'text-ok'
  if (q >= 0.5) return 'text-warn'
  return 'text-danger'
}
function qualityFillClass(q: number) {
  if (q >= 0.8) return 'qf-ok'
  if (q >= 0.5) return 'qf-warn'
  return 'qf-err'
}
function ttlClass(ttl: string) {
  if (ttl === '∞') return 'text-ok'
  if (ttl.includes('m') || ttl === '1h') return 'text-warn'
  return 'text-muted'
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/memory/list')
    entries.value = res.entries ?? res
    stats.value = res.stats ?? demoStats
  } catch {
    entries.value = demoEntries
    stats.value = demoStats
  } finally {
    loading.value = false
  }
}

async function pruneMemory() {
  pruning.value = true
  try {
    const res = await api<any>('/memory/prune', { method: 'POST' })
    store.showToast(`Pruned ${res.removed ?? 'some'} low-quality entries`, 'ok')
    await loadData()
  } catch {
    store.showToast('Prune failed', 'err')
  } finally {
    pruning.value = false
  }
}

async function analyzeMemory() {
  analyzing.value = true
  try {
    const res = await api<any>('/memory/analyze', { method: 'POST' })
    stats.value = res.stats ?? stats.value
    store.showToast('Analysis complete', 'ok')
  } catch {
    store.showToast('Analysis failed', 'err')
  } finally {
    analyzing.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.memory-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.kpi-card { flex: 1; min-width: 120px; padding: .75rem 1rem; }
.kpi-label { font-size: .7rem; letter-spacing: .06em; margin-bottom: .25rem; }
.kpi-value { font-size: 1.75rem; font-weight: 700; font-family: var(--font-mono); }

.tab-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  padding: .4rem .8rem;
  cursor: pointer;
  font-size: .88rem;
  border-bottom: 2px solid transparent;
  transition: var(--transition);
  display: flex; align-items: center;
}
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }

.search-input {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .4rem .65rem;
  font-size: .88rem;
  width: 300px;
  outline: none;
  transition: var(--transition);
}
.search-input:focus { border-color: var(--accent); }

.quality-bar-wrap { width: 80px; height: 4px; background: var(--bg3); border-radius: 2px; margin-bottom: .2rem; }
.quality-bar-fill { height: 100%; border-radius: 2px; transition: width .4s; }
.qf-ok { background: var(--accent3); }
.qf-warn { background: var(--warn); }
.qf-err { background: var(--danger); }
</style>
