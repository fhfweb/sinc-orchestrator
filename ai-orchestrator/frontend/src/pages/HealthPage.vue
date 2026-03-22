<template>
  <div class="health-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Health Grid</h1>
        <p class="text-muted">System-wide service health — auto-refreshes every 15s</p>
      </div>
      <div class="flex gap-2 items-center">
        <span class="text-muted" style="font-size:.8rem;">Next refresh in {{ countdown }}s</span>
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? 'Refreshing...' : 'Refresh Now' }}
        </button>
      </div>
    </div>

    <!-- Overall Score -->
    <div class="card overall-card flex items-center gap-2" style="margin-bottom:1.5rem;">
      <div class="overall-score" :class="overallScoreClass">{{ overallScore }}%</div>
      <div>
        <div style="font-weight:700; font-size:1rem;">Overall Health</div>
        <div class="text-muted" style="font-size:.82rem;">
          {{ healthyCount }} healthy · {{ degradedCount }} degraded · {{ downCount }} down of {{ services.length }} services
        </div>
      </div>
      <div style="margin-left:auto; text-align:right;">
        <div class="text-muted" style="font-size:.75rem;">LAST UPDATED</div>
        <div class="mono text-accent" style="font-size:.85rem;">{{ lastUpdated }}</div>
      </div>
    </div>

    <!-- Groups -->
    <div v-for="group in groups" :key="group.name" class="service-group" style="margin-bottom:1.5rem;">
      <div class="group-header flex items-center gap-2">
        <span class="group-name">{{ group.name }}</span>
        <span class="badge" :class="groupBadge(group)">{{ groupServicesLabel(group) }}</span>
      </div>
      <div class="services-grid">
        <div
          v-for="svc in group.services"
          :key="svc.id"
          class="service-cell card"
          :class="cellClass(svc.status)"
        >
          <div class="cell-header flex items-center justify-between">
            <span class="svc-name">{{ svc.name }}</span>
            <span class="status-dot" :class="'sdot-' + svc.status"></span>
          </div>
          <div class="cell-rt mono" :class="rtClass(svc.responseTime)">{{ svc.responseTime }}ms</div>
          <div class="cell-uptime text-muted">{{ svc.uptime }}% uptime</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const store = useAppStore()

interface Service {
  id: string
  name: string
  status: 'ok' | 'warn' | 'down'
  responseTime: number
  uptime: number
}
interface Group { name: string; services: Service[] }

const loading = ref(false)
const countdown = ref(15)
const lastUpdated = ref('')
const services = ref<Service[]>([])
const groups = ref<Group[]>([])

const demoGroups: Group[] = [
  {
    name: 'API Layer',
    services: [
      { id: 'api1', name: 'REST Gateway', status: 'ok', responseTime: 42, uptime: 99.98 },
      { id: 'api2', name: 'WebSocket Server', status: 'ok', responseTime: 8, uptime: 99.95 },
      { id: 'api3', name: 'Auth Service', status: 'ok', responseTime: 18, uptime: 100 },
      { id: 'api4', name: 'Rate Limiter', status: 'warn', responseTime: 245, uptime: 99.7 },
    ]
  },
  {
    name: 'Databases',
    services: [
      { id: 'db1', name: 'PostgreSQL', status: 'ok', responseTime: 3, uptime: 99.99 },
      { id: 'db2', name: 'Redis', status: 'ok', responseTime: 1, uptime: 100 },
      { id: 'db3', name: 'Qdrant', status: 'ok', responseTime: 12, uptime: 99.92 },
      { id: 'db4', name: 'ClickHouse', status: 'warn', responseTime: 380, uptime: 99.4 },
    ]
  },
  {
    name: 'AI Services',
    services: [
      { id: 'ai1', name: 'LLM Router', status: 'ok', responseTime: 94, uptime: 99.88 },
      { id: 'ai2', name: 'RAG Engine', status: 'ok', responseTime: 210, uptime: 99.75 },
      { id: 'ai3', name: 'Embedding Service', status: 'ok', responseTime: 55, uptime: 99.9 },
      { id: 'ai4', name: 'Agent Orchestrator', status: 'ok', responseTime: 30, uptime: 99.95 },
    ]
  },
  {
    name: 'Infrastructure',
    services: [
      { id: 'inf1', name: 'Docker Host', status: 'ok', responseTime: 0, uptime: 100 },
      { id: 'inf2', name: 'Message Queue', status: 'ok', responseTime: 5, uptime: 99.98 },
      { id: 'inf3', name: 'Object Storage', status: 'down', responseTime: 0, uptime: 98.2 },
    ]
  }
]

const healthyCount = computed(() => services.value.filter(s => s.status === 'ok').length)
const degradedCount = computed(() => services.value.filter(s => s.status === 'warn').length)
const downCount = computed(() => services.value.filter(s => s.status === 'down').length)
const overallScore = computed(() => {
  if (!services.value.length) return 100
  return Math.round((healthyCount.value / services.value.length) * 100)
})
const overallScoreClass = computed(() => {
  const s = overallScore.value
  if (s >= 90) return 'score-ok'
  if (s >= 70) return 'score-warn'
  return 'score-err'
})

function groupBadge(g: Group) {
  const has = (s: string) => g.services.some(svc => svc.status === s)
  if (has('down')) return 'badge-err'
  if (has('warn')) return 'badge-warn'
  return 'badge-ok'
}
function groupServicesLabel(g: Group) {
  const ok = g.services.filter(s => s.status === 'ok').length
  return `${ok}/${g.services.length} ok`
}
function cellClass(status: string) {
  if (status === 'ok') return 'cell-ok'
  if (status === 'warn') return 'cell-warn'
  return 'cell-down'
}
function rtClass(ms: number) {
  if (ms === 0) return 'text-muted'
  if (ms < 100) return 'text-ok'
  if (ms < 300) return 'text-warn'
  return 'text-danger'
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/health/grid')
    groups.value = res.groups ?? demoGroups
  } catch {
    groups.value = demoGroups
  } finally {
    loading.value = false
    lastUpdated.value = new Date().toLocaleTimeString()
    services.value = groups.value.flatMap(g => g.services)
    countdown.value = 15
  }
}

let countdownInterval: ReturnType<typeof setInterval>
let refreshInterval: ReturnType<typeof setInterval>

onMounted(() => {
  loadData()
  countdownInterval = setInterval(() => {
    countdown.value = Math.max(0, countdown.value - 1)
  }, 1000)
  refreshInterval = setInterval(loadData, 15000)
})
onUnmounted(() => {
  clearInterval(countdownInterval)
  clearInterval(refreshInterval)
})
</script>

<style scoped>
.health-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.overall-card { padding: 1rem 1.5rem; }
.overall-score {
  font-size: 3rem;
  font-weight: 900;
  font-family: var(--font-mono);
  line-height: 1;
  flex-shrink: 0;
}
.score-ok { color: var(--accent3); }
.score-warn { color: var(--warn); }
.score-err { color: var(--danger); }

.group-header { margin-bottom: .75rem; }
.group-name { font-weight: 700; font-size: .95rem; }

.services-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: .75rem;
}

.service-cell {
  padding: .75rem 1rem;
  border-left: 3px solid var(--panel-border);
  transition: var(--transition);
}
.cell-ok { border-left-color: var(--accent3); }
.cell-warn { border-left-color: var(--warn); }
.cell-down { border-left-color: var(--danger); opacity: .75; }

.svc-name { font-size: .85rem; font-weight: 600; }
.cell-rt { font-size: 1.1rem; font-weight: 700; margin: .35rem 0 .15rem; }
.cell-uptime { font-size: .75rem; }

.status-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.sdot-ok { background: var(--accent3); box-shadow: 0 0 5px var(--accent3); }
.sdot-warn { background: var(--warn); box-shadow: 0 0 5px var(--warn); }
.sdot-down { background: var(--danger); box-shadow: 0 0 5px var(--danger); }
</style>
