<template>
  <div class="twin-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Digital Twin</h1>
        <p class="text-muted">Virtual mirror of the production environment for simulation and drift detection</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-primary" @click="syncNow" :disabled="syncing">
          {{ syncing ? 'Syncing...' : 'Sync Now' }}
        </button>
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? '...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <!-- Status Hero -->
    <div class="card status-hero flex items-center gap-2" style="margin-bottom:1.5rem; padding:1.5rem;">
      <div class="twin-status-indicator" :class="'tsi-' + twin.status"></div>
      <div>
        <div class="twin-status-text" :class="statusTextClass">{{ statusLabel }}</div>
        <div class="text-muted" style="font-size:.85rem;">Last synchronized: {{ twin.lastSync }}</div>
      </div>
      <div class="twin-meta" style="margin-left:auto; text-align:right;">
        <div class="text-muted" style="font-size:.7rem;">DRIFT SCORE</div>
        <div class="drift-score mono" :class="driftScoreClass">{{ twin.driftScore }}</div>
      </div>
    </div>

    <!-- Summary Cards -->
    <div class="flex gap-2" style="margin-bottom:1.5rem; flex-wrap:wrap;">
      <div class="card kpi-card">
        <div class="kpi-label text-muted">DRIFT ITEMS</div>
        <div class="kpi-value" :class="twin.driftItems > 0 ? 'text-warn' : 'text-ok'">{{ twin.driftItems }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">IN SYNC</div>
        <div class="kpi-value text-ok">{{ twin.inSyncCount }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">SIMULATIONS RUN</div>
        <div class="kpi-value text-accent">{{ twin.simulationsRun }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">SYNC INTERVAL</div>
        <div class="kpi-value text-muted" style="font-size:1.2rem;">{{ twin.syncInterval }}</div>
      </div>
    </div>

    <!-- Drift Report -->
    <div class="card" style="margin-bottom:1.5rem;">
      <div class="section-title flex items-center justify-between">
        <span>Drift Report</span>
        <span class="badge" :class="twin.driftItems > 0 ? 'badge-warn' : 'badge-ok'">
          {{ twin.driftItems > 0 ? twin.driftItems + ' differences' : 'Fully in sync' }}
        </span>
      </div>
      <div v-if="twin.driftReport.length === 0" class="text-muted" style="padding:1.5rem; text-align:center;">
        No drift detected — twin is fully synchronized with production
      </div>
      <table v-else class="tbl">
        <thead>
          <tr>
            <th>Component</th>
            <th>Field</th>
            <th>Production Value</th>
            <th>Twin Value</th>
            <th>Severity</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="drift in twin.driftReport" :key="drift.id">
            <td style="font-weight:600; font-size:.88rem;">{{ drift.component }}</td>
            <td class="mono text-muted" style="font-size:.8rem;">{{ drift.field }}</td>
            <td class="mono text-ok" style="font-size:.8rem;">{{ drift.prodValue }}</td>
            <td class="mono text-warn" style="font-size:.8rem;">{{ drift.twinValue }}</td>
            <td><span class="badge" :class="severityBadge(drift.severity)">{{ drift.severity }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Use Cases -->
    <div class="card">
      <div class="section-title">Digital Twin Use Cases</div>
      <div class="use-cases-grid">
        <div class="use-case-card" v-for="uc in useCases" :key="uc.title">
          <div class="uc-icon">{{ uc.icon }}</div>
          <div class="uc-title">{{ uc.title }}</div>
          <div class="uc-desc text-muted">{{ uc.description }}</div>
        </div>
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

interface DriftItem { id: string; component: string; field: string; prodValue: string; twinValue: string; severity: 'low' | 'medium' | 'high' }
interface Twin {
  status: 'in-sync' | 'drifted' | 'unknown'
  lastSync: string
  driftScore: number
  driftItems: number
  inSyncCount: number
  simulationsRun: number
  syncInterval: string
  driftReport: DriftItem[]
}

const loading = ref(false)
const syncing = ref(false)

const twin = ref<Twin>({
  status: 'drifted',
  lastSync: '2026-03-22 13:55 UTC',
  driftScore: 12,
  driftItems: 3,
  inSyncCount: 42,
  simulationsRun: 284,
  syncInterval: '5 min',
  driftReport: [
    { id: 'd1', component: 'LLM Router', field: 'fallback_order', prodValue: '["openai","anthropic","groq"]', twinValue: '["openai","groq","anthropic"]', severity: 'low' },
    { id: 'd2', component: 'Token Budget', field: 'tenant-002.daily_limit', prodValue: '500000', twinValue: '450000', severity: 'medium' },
    { id: 'd3', component: 'Agent Config', field: 'planner.max_iterations', prodValue: '25', twinValue: '20', severity: 'low' },
  ]
})

const useCases = [
  { icon: '🧪', title: 'Safe Experimentation', description: 'Run experiments and load tests against the twin without affecting production users.' },
  { icon: '🔮', title: 'Failure Prediction', description: 'Simulate failure scenarios to predict blast radius and validate runbooks.' },
  { icon: '📐', title: 'Capacity Planning', description: 'Model traffic growth on the twin to plan infrastructure scaling.' },
  { icon: '🔄', title: 'Config Validation', description: 'Test configuration changes on the twin before applying them to production.' },
]

const statusLabel = computed(() => {
  if (twin.value.status === 'in-sync') return 'Fully In Sync'
  if (twin.value.status === 'drifted') return 'Drift Detected'
  return 'Status Unknown'
})
const statusTextClass = computed(() => {
  if (twin.value.status === 'in-sync') return 'text-ok'
  if (twin.value.status === 'drifted') return 'text-warn'
  return 'text-muted'
})
const driftScoreClass = computed(() => {
  const s = twin.value.driftScore
  if (s === 0) return 'text-ok'
  if (s < 30) return 'text-warn'
  return 'text-danger'
})

function severityBadge(s: string) {
  if (s === 'high') return 'badge-err'
  if (s === 'medium') return 'badge-warn'
  return 'badge-info'
}

async function syncNow() {
  syncing.value = true
  try {
    const res = await api<any>('/twin/sync', { method: 'POST' })
    twin.value = res
    store.showToast('Twin synchronized with production', 'ok')
  } catch {
    twin.value = { ...twin.value, status: 'in-sync', lastSync: new Date().toUTCString(), driftItems: 0, driftReport: [], driftScore: 0, inSyncCount: 45 }
    store.showToast('Sync complete (demo)', 'ok')
  } finally {
    syncing.value = false
  }
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/twin/status')
    twin.value = res
  } catch {
    // keep demo data
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.twin-page { padding: 1.5rem; max-width: 1200px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.status-hero { }
.twin-status-indicator {
  width: 20px; height: 20px;
  border-radius: 50%;
  flex-shrink: 0;
}
.tsi-in-sync { background: var(--accent3); box-shadow: 0 0 12px var(--accent3); }
.tsi-drifted { background: var(--warn); box-shadow: 0 0 12px var(--warn); animation: pulse-warn 2s infinite; }
.tsi-unknown { background: var(--text-muted); }

@keyframes pulse-warn {
  0%, 100% { box-shadow: 0 0 6px var(--warn); }
  50% { box-shadow: 0 0 18px var(--warn); }
}

.twin-status-text { font-size: 1.3rem; font-weight: 700; }
.drift-score { font-size: 2rem; font-weight: 900; }

.kpi-card { flex: 1; min-width: 130px; padding: .75rem 1rem; }
.kpi-label { font-size: .7rem; letter-spacing: .06em; margin-bottom: .25rem; }
.kpi-value { font-size: 1.75rem; font-weight: 700; font-family: var(--font-mono); }

.section-title { font-weight: 700; font-size: .9rem; padding: .75rem 1rem; border-bottom: 1px solid var(--panel-border); }

.use-cases-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 1rem;
  padding: 1rem;
}
.use-case-card {
  padding: 1rem;
  background: var(--bg2);
  border-radius: var(--radius);
  border: 1px solid var(--panel-border);
}
.uc-icon { font-size: 1.5rem; margin-bottom: .5rem; }
.uc-title { font-weight: 700; font-size: .9rem; margin-bottom: .35rem; }
.uc-desc { font-size: .8rem; line-height: 1.5; }
</style>
