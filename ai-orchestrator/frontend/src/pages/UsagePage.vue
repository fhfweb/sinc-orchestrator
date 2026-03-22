<template>
  <div class="usage-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Usage Analytics</h1>
        <p class="text-muted">API consumption, token usage and cost analysis</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-ghost" @click="exportCsv">Export CSV</button>
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? '...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <!-- KPI Row -->
    <div class="flex gap-2" style="margin-bottom:1.5rem; flex-wrap:wrap;">
      <div class="card kpi-card">
        <div class="kpi-label text-muted">TOTAL API CALLS (24H)</div>
        <div class="kpi-value text-accent">{{ analytics.totalCalls.toLocaleString() }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">TOKENS CONSUMED</div>
        <div class="kpi-value text-accent2">{{ (analytics.totalTokens / 1000).toFixed(0) }}K</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">ACTIVE USERS</div>
        <div class="kpi-value text-ok">{{ analytics.activeUsers }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">TOTAL COST (24H)</div>
        <div class="kpi-value text-warn">${{ analytics.totalCost.toFixed(2) }}</div>
      </div>
    </div>

    <!-- API Calls per Hour Chart -->
    <div class="card" style="margin-bottom:1.5rem; padding:1.25rem;">
      <div class="chart-title">API Calls per Hour — Last 24 Hours</div>
      <div class="bar-chart">
        <div class="bar-y-axis">
          <span>{{ maxHourly }}</span>
          <span>{{ Math.round(maxHourly * .75) }}</span>
          <span>{{ Math.round(maxHourly * .5) }}</span>
          <span>{{ Math.round(maxHourly * .25) }}</span>
          <span>0</span>
        </div>
        <div class="bar-area">
          <div
            v-for="(h, i) in analytics.hourly"
            :key="i"
            class="bar-col"
          >
            <div
              class="bar-fill bar-calls"
              :style="{ height: (h.calls / maxHourly * 100) + '%' }"
              :title="h.hour + ': ' + h.calls + ' calls'"
            ></div>
            <div class="bar-label text-muted">{{ h.hour }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Token Consumption Chart -->
    <div class="card" style="margin-bottom:1.5rem; padding:1.25rem;">
      <div class="chart-title">Token Consumption per Hour (K tokens)</div>
      <div class="bar-chart">
        <div class="bar-y-axis">
          <span>{{ maxTokenHourly }}K</span>
          <span>{{ Math.round(maxTokenHourly * .5) }}K</span>
          <span>0</span>
        </div>
        <div class="bar-area">
          <div
            v-for="(h, i) in analytics.hourly"
            :key="i"
            class="bar-col"
          >
            <div
              class="bar-fill bar-tokens"
              :style="{ height: (h.tokens / (maxTokenHourly * 1000) * 100) + '%' }"
              :title="h.hour + ': ' + (h.tokens/1000).toFixed(1) + 'K tokens'"
            ></div>
            <div class="bar-label text-muted">{{ h.hour }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Per-Endpoint Table -->
    <div class="card" style="overflow-x:auto;">
      <div class="section-header flex items-center justify-between">
        <span class="section-title">Per-Endpoint Usage</span>
      </div>
      <table class="tbl">
        <thead>
          <tr>
            <th>Endpoint</th>
            <th>Method</th>
            <th>Calls</th>
            <th>Avg Latency</th>
            <th>P99 Latency</th>
            <th>Error Rate</th>
            <th>Tokens/Call</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="ep in analytics.endpoints" :key="ep.path">
            <td class="mono" style="font-size:.8rem;">{{ ep.path }}</td>
            <td><span class="badge" :class="methodBadge(ep.method)">{{ ep.method }}</span></td>
            <td class="mono text-accent">{{ ep.calls.toLocaleString() }}</td>
            <td class="mono" :class="latencyClass(ep.avgLatency)">{{ ep.avgLatency }}ms</td>
            <td class="mono" :class="latencyClass(ep.p99Latency)">{{ ep.p99Latency }}ms</td>
            <td class="mono" :class="errorClass(ep.errorRate)">{{ ep.errorRate.toFixed(1) }}%</td>
            <td class="mono text-muted">{{ ep.tokensPerCall.toLocaleString() }}</td>
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

interface HourlyData { hour: string; calls: number; tokens: number; cost: number }
interface EndpointData { path: string; method: string; calls: number; avgLatency: number; p99Latency: number; errorRate: number; tokensPerCall: number }
interface Analytics {
  totalCalls: number
  totalTokens: number
  activeUsers: number
  totalCost: number
  hourly: HourlyData[]
  endpoints: EndpointData[]
}

const loading = ref(false)
const analytics = ref<Analytics>({
  totalCalls: 0, totalTokens: 0, activeUsers: 0, totalCost: 0, hourly: [], endpoints: []
})

function genHourly(): HourlyData[] {
  return Array.from({ length: 24 }, (_, i) => {
    const hour = String(i).padStart(2, '0') + ':00'
    const base = 200 + Math.random() * 800
    return { hour, calls: Math.round(base), tokens: Math.round(base * 420), cost: base * 0.0008 }
  })
}

const demoAnalytics: Analytics = {
  totalCalls: 28430,
  totalTokens: 11420000,
  activeUsers: 47,
  totalCost: 34.82,
  hourly: genHourly(),
  endpoints: [
    { path: '/api/v5/dashboard/llm/status', method: 'GET', calls: 8240, avgLatency: 45, p99Latency: 210, errorRate: 0.2, tokensPerCall: 0 },
    { path: '/api/v5/dashboard/ask', method: 'POST', calls: 6120, avgLatency: 1240, p99Latency: 3800, errorRate: 1.1, tokensPerCall: 1840 },
    { path: '/api/v5/dashboard/intelligence', method: 'POST', calls: 4380, avgLatency: 2100, p99Latency: 5200, errorRate: 0.8, tokensPerCall: 2420 },
    { path: '/api/v5/dashboard/memory/list', method: 'GET', calls: 3210, avgLatency: 92, p99Latency: 380, errorRate: 0.0, tokensPerCall: 0 },
    { path: '/api/v5/dashboard/context/traces', method: 'GET', calls: 2980, avgLatency: 118, p99Latency: 420, errorRate: 0.3, tokensPerCall: 0 },
    { path: '/api/v5/dashboard/token-budgets', method: 'GET', calls: 1820, avgLatency: 38, p99Latency: 145, errorRate: 0.0, tokensPerCall: 0 },
    { path: '/api/v5/dashboard/health/grid', method: 'GET', calls: 1680, avgLatency: 55, p99Latency: 190, errorRate: 0.0, tokensPerCall: 0 },
  ]
}

const maxHourly = computed(() => Math.max(...analytics.value.hourly.map(h => h.calls), 1))
const maxTokenHourly = computed(() => Math.ceil(Math.max(...analytics.value.hourly.map(h => h.tokens), 1) / 1000))

function methodBadge(m: string) {
  if (m === 'GET') return 'badge-ok'
  if (m === 'POST') return 'badge-info'
  if (m === 'DELETE') return 'badge-err'
  return 'badge-warn'
}
function latencyClass(ms: number) {
  if (ms < 200) return 'text-ok'
  if (ms < 1000) return 'text-warn'
  return 'text-danger'
}
function errorClass(r: number) {
  if (r < 1) return 'text-ok'
  if (r < 5) return 'text-warn'
  return 'text-danger'
}

function exportCsv() {
  const rows = [
    ['Endpoint', 'Method', 'Calls', 'Avg Latency', 'P99 Latency', 'Error Rate', 'Tokens/Call'],
    ...analytics.value.endpoints.map(e => [e.path, e.method, e.calls, e.avgLatency, e.p99Latency, e.errorRate, e.tokensPerCall])
  ]
  const csv = rows.map(r => r.join(',')).join('\n')
  const blob = new Blob([csv], { type: 'text/csv' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = 'usage-analytics.csv'
  a.click()
  store.showToast('CSV downloaded', 'ok')
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/usage/analytics')
    analytics.value = res
  } catch {
    analytics.value = demoAnalytics
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.usage-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.kpi-card { flex: 1; min-width: 160px; padding: .75rem 1rem; }
.kpi-label { font-size: .7rem; letter-spacing: .06em; margin-bottom: .25rem; }
.kpi-value { font-size: 1.75rem; font-weight: 700; font-family: var(--font-mono); }

.chart-title { font-weight: 700; font-size: .88rem; margin-bottom: .75rem; }

.bar-chart {
  display: flex;
  gap: .35rem;
  height: 160px;
  align-items: flex-end;
}
.bar-y-axis {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: flex-end;
  font-size: .62rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
  height: 120px;
  padding-right: .35rem;
  flex-shrink: 0;
}
.bar-area {
  display: flex;
  gap: .15rem;
  align-items: flex-end;
  flex: 1;
  height: 120px;
  border-bottom: 1px solid var(--panel-border);
  border-left: 1px solid var(--panel-border);
  padding: 0 .25rem;
  overflow: hidden;
}
.bar-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  height: 100%;
  justify-content: flex-end;
  gap: .15rem;
  min-width: 0;
}
.bar-fill {
  width: 100%;
  border-radius: 2px 2px 0 0;
  transition: height .4s ease;
}
.bar-calls { background: var(--accent); opacity: .8; }
.bar-tokens { background: var(--accent2); opacity: .8; }
.bar-label { font-size: .55rem; color: var(--text-muted); font-family: var(--font-mono); }

.section-header { padding: .75rem 1rem; border-bottom: 1px solid var(--panel-border); }
.section-title { font-weight: 700; font-size: .88rem; }
</style>
