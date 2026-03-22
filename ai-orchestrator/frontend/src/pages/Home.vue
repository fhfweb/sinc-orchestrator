<template>
  <div class="home-page">
    <!-- KPI Row -->
    <div class="kpi-row">
      <div class="kpi-card card" v-for="k in kpis" :key="k.label">
        <div class="kpi-val" :class="k.cls">{{ k.val }}</div>
        <div class="kpi-label text-muted">{{ k.label }}</div>
        <div class="kpi-delta" :class="k.delta >= 0 ? 'text-ok' : 'text-danger'">
          {{ k.delta >= 0 ? '▲' : '▼' }} {{ Math.abs(k.delta) }}%
        </div>
      </div>
    </div>

    <!-- Main grid -->
    <div class="home-grid">
      <!-- Agent Roster -->
      <section class="card roster-card">
        <div class="card-header flex justify-between items-center">
          <span class="card-title">Agent Roster</span>
          <button class="btn btn-ghost" @click="loadRoster">↻</button>
        </div>
        <div class="roster-grid">
          <div
            v-for="a in agents"
            :key="a.id"
            class="roster-agent"
            :class="`status-${a.status}`"
          >
            <span class="ra-dot"></span>
            <div class="ra-info">
              <div class="ra-name truncate">{{ a.name }}</div>
              <div class="ra-task truncate text-muted">{{ a.current_task ?? 'idle' }}</div>
            </div>
            <span class="ra-status badge" :class="badgeCls(a.status)">{{ a.status }}</span>
          </div>
        </div>
      </section>

      <!-- System Health -->
      <section class="card health-card">
        <div class="card-header">
          <span class="card-title">Status Operacional</span>
        </div>
        <div class="health-grid">
          <div v-for="s in services" :key="s.name" class="svc-row">
            <span class="dot" :class="s.status === 'ok' ? 'ok' : s.status === 'warn' ? 'warn' : 'err'"></span>
            <span class="svc-name">{{ s.name }}</span>
            <span class="svc-val text-muted mono">{{ s.val }}</span>
          </div>
        </div>

        <div class="sla-row mt-4">
          <div class="sla-item">
            <div class="sla-val text-ok">{{ sla.uptime }}%</div>
            <div class="sla-label text-muted">SLA Uptime</div>
          </div>
          <div class="sla-item">
            <div class="sla-val" :class="sla.incidents > 0 ? 'text-warn' : 'text-ok'">{{ sla.incidents }}</div>
            <div class="sla-label text-muted">Incidents</div>
          </div>
          <div class="sla-item">
            <div class="sla-val" :class="sla.alerts > 0 ? 'text-warn' : 'text-ok'">{{ sla.alerts }}</div>
            <div class="sla-label text-muted">Alertas</div>
          </div>
        </div>
      </section>

      <!-- RED Metrics mini -->
      <section class="card red-card">
        <div class="card-header">
          <span class="card-title">RED Metrics</span>
          <RouterLink to="/noc/metrics" class="text-muted" style="font-size:11px">ver mais →</RouterLink>
        </div>
        <div class="red-metrics">
          <div class="red-item">
            <div class="red-val text-accent mono">{{ red.rps }}<span class="red-unit">r/s</span></div>
            <div class="red-label">Rate</div>
          </div>
          <div class="red-item">
            <div class="red-val mono" :class="red.errPct > 1 ? 'text-danger' : 'text-ok'">{{ red.errPct }}<span class="red-unit">%</span></div>
            <div class="red-label">Errors</div>
          </div>
          <div class="red-item">
            <div class="red-val text-warn mono">{{ red.p99 }}<span class="red-unit">ms</span></div>
            <div class="red-label">P99</div>
          </div>
        </div>
      </section>

      <!-- Quick links -->
      <section class="card quick-card">
        <div class="card-header"><span class="card-title">Ações Rápidas</span></div>
        <div class="quick-links">
          <RouterLink v-for="q in quickLinks" :key="q.path" :to="q.path" class="ql-item">
            <span class="ql-icon">{{ q.icon }}</span>
            <span>{{ q.label }}</span>
          </RouterLink>
        </div>
      </section>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const app = useAppStore()

const kpis = ref([
  { label: 'Agentes Ativos', val: '—', delta: 0, cls: 'text-accent' },
  { label: 'Tasks/h', val: '—', delta: 0, cls: '' },
  { label: 'Erro Rate', val: '—', delta: 0, cls: '' },
  { label: 'Latência P99', val: '—', delta: 0, cls: '' }
])

interface Agent {
  id: string
  name: string
  status: 'idle' | 'thinking' | 'executing' | 'error'
  current_task?: string
}
const agents = ref<Agent[]>([])

const services = ref([
  { name: 'API Gateway', status: 'ok', val: '12ms' },
  { name: 'Redis', status: 'ok', val: '0.3ms' },
  { name: 'PostgreSQL', status: 'ok', val: '4ms' },
  { name: 'Qdrant', status: 'ok', val: '8ms' },
  { name: 'LLM Router', status: 'ok', val: '340ms' }
])

const sla = ref({ uptime: 99.9, incidents: 0, alerts: 0 })
const red = ref({ rps: 0, errPct: 0, p99: 0 })

const quickLinks = [
  { path: '/noc/kanban', label: 'Job Board', icon: '⊞' },
  { path: '/noc/ask', label: 'Ask N5', icon: '✦' },
  { path: '/noc/logs', label: 'Live Logs', icon: '▶' },
  { path: '/noc/agents', label: 'Agentes', icon: '◈' },
  { path: '/noc/chaos', label: 'Chaos Eng', icon: '⚡' },
  { path: '/noc/compliance', label: 'Compliance', icon: '⊛' }
]

function badgeCls(status: string) {
  return { 'badge-ok': status === 'idle', 'badge-info': status === 'thinking', 'badge-warn': status === 'executing', 'badge-err': status === 'error' }
}

async function loadRoster() {
  try {
    const data = await api<{ agents: Agent[] }>('/agents/roster')
    agents.value = data.agents ?? []
  } catch {
    // fallback demo data
    agents.value = [
      { id: '1', name: 'Orchestrator', status: 'executing', current_task: 'task-dispatch' },
      { id: '2', name: 'RAG-Engine', status: 'idle' },
      { id: '3', name: 'LLM-Router', status: 'thinking', current_task: 'gpt-4o-mini' },
      { id: '4', name: 'Memory-Sync', status: 'idle' },
      { id: '5', name: 'Cognitive-Core', status: 'executing', current_task: 'mcts-search' },
      { id: '6', name: 'Supervisor', status: 'idle' }
    ]
  }
}

async function loadMetrics() {
  try {
    const data = await api<Record<string, number>>('/metrics/red')
    red.value.rps = data.rps ?? 0
    red.value.errPct = data.error_rate ?? 0
    red.value.p99 = data.p99_ms ?? 0
    kpis.value[2].val = `${red.value.errPct}%`
    kpis.value[3].val = `${red.value.p99}ms`
  } catch { /* silent */ }
  try {
    const sv = await api<{ active_count: number }>('/agents/active-count')
    kpis.value[0].val = String(sv.active_count ?? '—')
  } catch { /* silent */ }
}

let interval: ReturnType<typeof setInterval>
onMounted(() => {
  loadRoster()
  loadMetrics()
  interval = setInterval(loadMetrics, 10_000)
})
onUnmounted(() => clearInterval(interval))
</script>

<style scoped>
.home-page { display: flex; flex-direction: column; gap: 16px; }

.kpi-row {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.kpi-card { text-align: center; }
.kpi-val { font-size: 28px; font-weight: 700; font-family: var(--font-mono); }
.kpi-label { font-size: 11px; margin-top: 2px; }
.kpi-delta { font-size: 11px; margin-top: 4px; }

.home-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  grid-template-rows: auto auto;
  gap: 12px;
}

.card-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }

/* Roster */
.roster-grid { display: flex; flex-direction: column; gap: 6px; }
.roster-agent {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: var(--radius-sm);
  background: var(--bg3);
}
.ra-dot {
  width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  background: var(--text-muted);
}
.status-idle .ra-dot { background: var(--text-muted); }
.status-thinking .ra-dot { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.status-executing .ra-dot { background: var(--accent3); box-shadow: 0 0 6px var(--accent3); animation: pulse 1.2s infinite; }
.status-error .ra-dot { background: var(--danger); box-shadow: 0 0 6px var(--danger); }
@keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:0.4; } }
.ra-info { flex: 1; min-width: 0; }
.ra-name { font-size: 12px; font-weight: 500; }
.ra-task { font-size: 11px; }
.ra-status { font-size: 10px; }

/* Health */
.health-grid { display: flex; flex-direction: column; gap: 6px; }
.svc-row { display: flex; align-items: center; gap: 8px; }
.svc-name { flex: 1; font-size: 12px; }
.svc-val { font-size: 11px; }
.sla-row { display: flex; gap: 24px; }
.sla-item { text-align: center; }
.sla-val { font-size: 20px; font-weight: 700; font-family: var(--font-mono); }
.sla-label { font-size: 10px; margin-top: 2px; }

/* RED */
.red-metrics { display: flex; gap: 24px; justify-content: center; padding: 12px 0; }
.red-item { text-align: center; }
.red-val { font-size: 32px; font-weight: 700; }
.red-unit { font-size: 12px; color: var(--text-muted); margin-left: 2px; }
.red-label { font-size: 11px; color: var(--text-muted); margin-top: 4px; text-transform: uppercase; letter-spacing: 0.5px; }

/* Quick links */
.quick-links {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}
.ql-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: var(--bg3);
  border-radius: var(--radius-sm);
  font-size: 12px;
  color: var(--text-muted);
  transition: var(--transition);
  text-decoration: none;
  border: 1px solid transparent;
}
.ql-item:hover { border-color: var(--accent); color: var(--accent); text-decoration: none; }
.ql-icon { font-size: 14px; }

@media (max-width: 900px) {
  .kpi-row { grid-template-columns: 1fr 1fr; }
  .home-grid { grid-template-columns: 1fr; }
}
</style>
