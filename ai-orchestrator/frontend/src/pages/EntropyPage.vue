<template>
  <div class="entropy-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Entropy Analysis</h1>
        <p class="text-muted">LLM output entropy and agent decision diversity metrics</p>
      </div>
      <button class="btn btn-ghost" @click="loadData" :disabled="loading">
        {{ loading ? 'Refreshing...' : 'Refresh' }}
      </button>
    </div>

    <!-- KPI Cards -->
    <div class="kpi-row flex gap-2" style="margin-bottom:1.5rem;">
      <div class="card kpi-card">
        <div class="text-muted kpi-label">MEAN ENTROPY</div>
        <div class="kpi-value text-accent">{{ stats.mean.toFixed(3) }}</div>
        <div class="text-muted" style="font-size:.7rem;">nats</div>
      </div>
      <div class="card kpi-card">
        <div class="text-muted kpi-label">STD DEVIATION</div>
        <div class="kpi-value text-accent2">{{ stats.std.toFixed(3) }}</div>
        <div class="text-muted" style="font-size:.7rem;">nats</div>
      </div>
      <div class="card kpi-card">
        <div class="text-muted kpi-label">MIN ENTROPY</div>
        <div class="kpi-value text-ok">{{ stats.min.toFixed(3) }}</div>
        <div class="text-muted" style="font-size:.7rem;">nats</div>
      </div>
      <div class="card kpi-card">
        <div class="text-muted kpi-label">MAX ENTROPY</div>
        <div class="kpi-value text-warn">{{ stats.max.toFixed(3) }}</div>
        <div class="text-muted" style="font-size:.7rem;">nats</div>
      </div>
    </div>

    <!-- Bar Chart -->
    <div class="card" style="margin-bottom:1.5rem; padding:1.25rem;">
      <div class="card-title">Entropy Over Time (last 10 measurements)</div>
      <div class="bar-chart">
        <div class="bar-chart-y-labels">
          <span>1.0</span>
          <span>0.75</span>
          <span>0.5</span>
          <span>0.25</span>
          <span>0</span>
        </div>
        <div class="bar-chart-bars">
          <div
            v-for="(point, i) in chartData"
            :key="i"
            class="bar-col"
          >
            <div class="bar-value mono">{{ point.value.toFixed(2) }}</div>
            <div
              class="bar-fill"
              :class="barColorClass(point.value)"
              :style="{ height: (point.value / 1.2 * 100) + '%' }"
              :title="point.label + ': ' + point.value.toFixed(3)"
            ></div>
            <div class="bar-label text-muted">{{ point.label }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- Measurements Table -->
    <div class="card" style="overflow-x:auto;">
      <div class="card-title" style="padding:.75rem 1rem; border-bottom:1px solid var(--panel-border);">
        Recent Measurements by Agent
      </div>
      <table class="tbl">
        <thead>
          <tr>
            <th>Timestamp</th>
            <th>Agent</th>
            <th>Entropy</th>
            <th>Perplexity</th>
            <th>Temperature</th>
            <th>Model</th>
            <th>Assessment</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="m in measurements" :key="m.id">
            <td class="mono text-muted" style="font-size:.75rem;">{{ m.timestamp }}</td>
            <td><span class="badge badge-info">{{ m.agent }}</span></td>
            <td class="mono" :class="entropyClass(m.entropy)">{{ m.entropy.toFixed(4) }}</td>
            <td class="mono">{{ m.perplexity.toFixed(2) }}</td>
            <td class="mono">{{ m.temperature.toFixed(1) }}</td>
            <td class="text-muted" style="font-size:.8rem;">{{ m.model }}</td>
            <td>
              <span class="badge" :class="assessmentBadge(m.entropy)">
                {{ assessmentLabel(m.entropy) }}
              </span>
            </td>
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

interface Measurement {
  id: string
  timestamp: string
  agent: string
  entropy: number
  perplexity: number
  temperature: number
  model: string
}

const loading = ref(false)
const measurements = ref<Measurement[]>([])

const demoMeasurements: Measurement[] = [
  { id: '1', timestamp: '2026-03-22 14:32:01', agent: 'PlannerAgent', entropy: 0.8821, perplexity: 2.416, temperature: 0.7, model: 'gpt-4o' },
  { id: '2', timestamp: '2026-03-22 14:31:45', agent: 'RAGAgent', entropy: 0.3214, perplexity: 1.379, temperature: 0.2, model: 'claude-sonnet-4-6' },
  { id: '3', timestamp: '2026-03-22 14:31:22', agent: 'ExecutorAgent', entropy: 0.1042, perplexity: 1.110, temperature: 0.0, model: 'gpt-4o' },
  { id: '4', timestamp: '2026-03-22 14:30:58', agent: 'MonitorAgent', entropy: 0.6530, perplexity: 1.921, temperature: 0.5, model: 'gemini-1.5-pro' },
  { id: '5', timestamp: '2026-03-22 14:30:30', agent: 'PlannerAgent', entropy: 0.9910, perplexity: 2.694, temperature: 0.9, model: 'gpt-4o' },
  { id: '6', timestamp: '2026-03-22 14:29:55', agent: 'RAGAgent', entropy: 0.4421, perplexity: 1.556, temperature: 0.3, model: 'claude-sonnet-4-6' },
  { id: '7', timestamp: '2026-03-22 14:29:12', agent: 'TwinAgent', entropy: 0.7734, perplexity: 2.167, temperature: 0.6, model: 'groq/llama-3.3' },
  { id: '8', timestamp: '2026-03-22 14:28:44', agent: 'ExecutorAgent', entropy: 0.2103, perplexity: 1.234, temperature: 0.1, model: 'gpt-4o' },
  { id: '9', timestamp: '2026-03-22 14:28:01', agent: 'MonitorAgent', entropy: 0.5512, perplexity: 1.735, temperature: 0.4, model: 'gemini-1.5-pro' },
  { id: '10', timestamp: '2026-03-22 14:27:33', agent: 'PlannerAgent', entropy: 0.8200, perplexity: 2.270, temperature: 0.7, model: 'gpt-4o' },
]

const chartData = computed(() => {
  return measurements.value.slice(0, 10).map((m, i) => ({
    label: `T-${10 - i}`,
    value: m.entropy
  }))
})

const stats = computed(() => {
  const vals = measurements.value.map(m => m.entropy)
  if (!vals.length) return { mean: 0, std: 0, min: 0, max: 0 }
  const mean = vals.reduce((a, b) => a + b, 0) / vals.length
  const variance = vals.reduce((a, b) => a + Math.pow(b - mean, 2), 0) / vals.length
  return {
    mean,
    std: Math.sqrt(variance),
    min: Math.min(...vals),
    max: Math.max(...vals)
  }
})

function entropyClass(e: number) {
  if (e < 0.3) return 'text-ok'
  if (e < 0.7) return 'text-warn'
  return 'text-danger'
}
function barColorClass(v: number) {
  if (v < 0.3) return 'bar-ok'
  if (v < 0.7) return 'bar-warn'
  return 'bar-err'
}
function assessmentLabel(e: number) {
  if (e < 0.3) return 'Deterministic'
  if (e < 0.7) return 'Moderate'
  return 'High Variance'
}
function assessmentBadge(e: number) {
  if (e < 0.3) return 'badge-ok'
  if (e < 0.7) return 'badge-warn'
  return 'badge-err'
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/entropy/metrics')
    measurements.value = res.measurements ?? res
  } catch {
    measurements.value = demoMeasurements
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.entropy-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.kpi-row { flex-wrap: wrap; }
.kpi-card { flex: 1; min-width: 140px; padding: .75rem 1rem; }
.kpi-label { font-size: .7rem; letter-spacing: .06em; margin-bottom: .25rem; }
.kpi-value { font-size: 2rem; font-weight: 700; font-family: var(--font-mono); line-height: 1; }

.card-title { font-weight: 700; font-size: .9rem; margin-bottom: .75rem; }

.bar-chart {
  display: flex;
  gap: .5rem;
  align-items: flex-end;
  height: 180px;
  padding-top: 1.5rem;
}
.bar-chart-y-labels {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: flex-end;
  font-size: .65rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
  height: 140px;
  padding-right: .4rem;
  flex-shrink: 0;
}
.bar-chart-bars {
  display: flex;
  gap: .4rem;
  align-items: flex-end;
  flex: 1;
  height: 140px;
  border-bottom: 1px solid var(--panel-border);
  border-left: 1px solid var(--panel-border);
  padding: 0 .5rem;
}
.bar-col {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  height: 100%;
  justify-content: flex-end;
  gap: .2rem;
}
.bar-value { font-size: .6rem; font-family: var(--font-mono); color: var(--text-muted); }
.bar-fill {
  width: 100%;
  max-width: 48px;
  border-radius: var(--radius-sm) var(--radius-sm) 0 0;
  transition: height .4s ease;
}
.bar-ok { background: var(--accent3); }
.bar-warn { background: var(--warn); }
.bar-err { background: var(--danger); }
.bar-label { font-size: .6rem; color: var(--text-muted); font-family: var(--font-mono); }
</style>
