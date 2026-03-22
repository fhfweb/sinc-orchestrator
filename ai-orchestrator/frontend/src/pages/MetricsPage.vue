<template>
  <div class="metrics-page">
    <div class="metrics-header flex justify-between items-center">
      <h1 class="page-title">Métricas RED</h1>
      <div class="flex items-center gap-2">
        <select v-model="window_">
          <option value="5m">5 min</option>
          <option value="15m">15 min</option>
          <option value="1h">1 hora</option>
          <option value="24h">24 horas</option>
        </select>
        <button class="btn btn-ghost" @click="load">↻</button>
      </div>
    </div>

    <!-- RED cards -->
    <div class="red-cards">
      <div class="red-card card">
        <div class="rc-label text-muted">Rate</div>
        <div class="rc-val text-accent mono">{{ metrics.rps }}<span class="rc-unit">req/s</span></div>
        <div class="rc-sub text-muted">{{ metrics.rpm }} req/min</div>
      </div>
      <div class="red-card card">
        <div class="rc-label text-muted">Errors</div>
        <div class="rc-val mono" :class="metrics.error_rate > 1 ? 'text-danger' : 'text-ok'">
          {{ metrics.error_rate }}<span class="rc-unit">%</span>
        </div>
        <div class="rc-sub text-muted">{{ metrics.errors_total }} erros totais</div>
      </div>
      <div class="red-card card">
        <div class="rc-label text-muted">Duration P50</div>
        <div class="rc-val text-warn mono">{{ metrics.p50 }}<span class="rc-unit">ms</span></div>
      </div>
      <div class="red-card card">
        <div class="rc-label text-muted">Duration P99</div>
        <div class="rc-val text-warn mono">{{ metrics.p99 }}<span class="rc-unit">ms</span></div>
      </div>
    </div>

    <!-- Endpoint table -->
    <div class="card mt-4">
      <div class="card-header"><span class="card-title">Endpoints</span></div>
      <table class="tbl">
        <thead>
          <tr><th>Endpoint</th><th>Rate</th><th>Errors%</th><th>P99</th><th>Status</th></tr>
        </thead>
        <tbody>
          <tr v-for="e in endpoints" :key="e.path">
            <td class="mono" style="font-size:11px">{{ e.path }}</td>
            <td>{{ e.rps }}/s</td>
            <td :class="e.err > 1 ? 'text-danger' : 'text-ok'">{{ e.err }}%</td>
            <td class="text-warn">{{ e.p99 }}ms</td>
            <td><span class="dot" :class="e.err > 5 ? 'err' : e.err > 1 ? 'warn' : 'ok'"></span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted, watch } from 'vue'
import { useApi } from '@/composables/useApi'

const { api } = useApi()
const window_ = ref('5m')

const metrics = ref({
  rps: 0, rpm: 0,
  error_rate: 0, errors_total: 0,
  p50: 0, p99: 0
})

const endpoints = ref([
  { path: '/api/v5/dashboard/agents/roster', rps: 0.3, err: 0, p99: 45 },
  { path: '/api/v5/dashboard/tasks/list', rps: 1.2, err: 0, p99: 80 },
  { path: '/api/v5/dashboard/ask', rps: 0.8, err: 0.5, p99: 340 },
  { path: '/api/v5/dashboard/metrics/red', rps: 2.0, err: 0, p99: 12 }
])

async function load() {
  try {
    const d = await api<typeof metrics.value>('/metrics/red', { params: { window: window_.value } })
    Object.assign(metrics.value, d)
  } catch { /* silent - show placeholder */ }
}

let interval: ReturnType<typeof setInterval>
onMounted(() => { load(); interval = setInterval(load, 15_000) })
onUnmounted(() => clearInterval(interval))
watch(window_, load)
</script>

<style scoped>
.metrics-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }

.red-cards {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
}
.red-card { text-align: center; padding: 20px; }
.rc-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }
.rc-val { font-size: 36px; font-weight: 800; }
.rc-unit { font-size: 14px; color: var(--text-muted); margin-left: 3px; }
.rc-sub { font-size: 11px; margin-top: 4px; }

.card-header { margin-bottom: 12px; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }

@media (max-width: 900px) {
  .red-cards { grid-template-columns: 1fr 1fr; }
}
</style>
