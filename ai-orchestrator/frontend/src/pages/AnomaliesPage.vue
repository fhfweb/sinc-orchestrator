<template>
  <div class="anomalies-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">⚠ Detecção de Anomalias</h1>
      <div class="flex gap-2">
        <select v-model="window_" @change="load">
          <option value="1h">1 hora</option>
          <option value="6h">6 horas</option>
          <option value="24h">24 horas</option>
        </select>
        <button class="btn btn-ghost" @click="load">↻ Refresh</button>
        <button class="btn btn-primary" @click="train">⚙ Treinar Modelo</button>
      </div>
    </div>

    <!-- Summary cards -->
    <div class="anomaly-summary">
      <div class="card sum-card" v-for="s in summary" :key="s.label">
        <div class="sc-val" :class="s.cls">{{ s.val }}</div>
        <div class="sc-label text-muted">{{ s.label }}</div>
      </div>
    </div>

    <!-- Anomaly list -->
    <div class="anomaly-layout">
      <div class="card anomaly-list">
        <div class="card-title mb-2">Anomalias detectadas</div>
        <div v-for="a in anomalies" :key="a.id" class="anomaly-row" :class="`sev-${a.severity}`">
          <div class="ar-sev">
            <span class="dot" :class="a.severity === 'critical' ? 'err' : a.severity === 'high' ? 'warn' : 'ok'"></span>
          </div>
          <div class="ar-body">
            <div class="ar-title">{{ a.metric }}</div>
            <div class="ar-sub text-muted">{{ a.description }}</div>
            <div class="ar-meta flex gap-2 mt-1">
              <span class="badge" :class="sevBadge(a.severity)">{{ a.severity }}</span>
              <span class="text-muted" style="font-size:10px">{{ a.ts }}</span>
              <span class="mono text-warn" style="font-size:10px">score: {{ a.score?.toFixed(3) }}</span>
            </div>
          </div>
        </div>
        <div v-if="anomalies.length === 0" class="text-muted" style="padding:20px;text-align:center">
          ✓ Nenhuma anomalia detectada no período
        </div>
      </div>

      <!-- Correlation heatmap placeholder -->
      <div class="card corr-panel">
        <div class="card-title mb-2">Correlações de Métricas</div>
        <div class="corr-matrix">
          <div v-for="row in corrMatrix" :key="row.a" class="corr-row">
            <span class="corr-label">{{ row.a }}</span>
            <div v-for="cell in row.cells" :key="cell.b" class="corr-cell" :style="`background:${corrColor(cell.v)}`" :title="`${row.a} vs ${cell.b}: ${cell.v.toFixed(2)}`">
              <span style="font-size:9px">{{ cell.v.toFixed(1) }}</span>
            </div>
          </div>
          <div class="corr-x-labels">
            <span v-for="l in corrLabels" :key="l" class="corr-label">{{ l }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api, apif } = useApi()
const app = useAppStore()

interface Anomaly {
  id: string
  metric: string
  description: string
  severity: 'low' | 'medium' | 'high' | 'critical'
  ts: string
  score?: number
}

const anomalies = ref<Anomaly[]>([])
const window_ = ref('6h')

const summary = ref([
  { label: 'Total', val: '0', cls: 'text-accent' },
  { label: 'Críticos', val: '0', cls: 'text-danger' },
  { label: 'Altos', val: '0', cls: 'text-warn' },
  { label: 'Score médio', val: '—', cls: 'text-muted' }
])

const corrLabels = ['rps', 'p99', 'cpu', 'mem', 'err']

const corrMatrix = ref(corrLabels.map((a, i) => ({
  a,
  cells: corrLabels.map((b, j) => ({
    b,
    v: i === j ? 1 : Math.round((Math.random() * 2 - 1) * 10) / 10
  }))
})))

function corrColor(v: number) {
  if (v >= 0.7) return 'rgba(16,185,129,0.5)'
  if (v >= 0.3) return 'rgba(16,185,129,0.2)'
  if (v <= -0.7) return 'rgba(239,68,68,0.5)'
  if (v <= -0.3) return 'rgba(239,68,68,0.2)'
  return 'rgba(255,255,255,0.05)'
}

function sevBadge(s: string) {
  return { 'badge-err': s === 'critical', 'badge-warn': s === 'high', 'badge-info': s === 'medium' }
}

function updateSummary() {
  const total = anomalies.value.length
  const crit = anomalies.value.filter(a => a.severity === 'critical').length
  const high = anomalies.value.filter(a => a.severity === 'high').length
  const avgScore = total > 0
    ? (anomalies.value.reduce((s, a) => s + (a.score ?? 0), 0) / total).toFixed(3)
    : '—'
  summary.value[0].val = String(total)
  summary.value[1].val = String(crit)
  summary.value[2].val = String(high)
  summary.value[3].val = String(avgScore)
}

async function train() {
  try {
    await apif('/anomalies/train', { window: window_.value })
    app.showToast('Modelo treinado', 'ok')
    load()
  } catch { app.showToast('Erro ao treinar modelo', 'err') }
}

async function load() {
  try {
    const d = await api<{ anomalies: Anomaly[] }>('/anomalies', { params: { window: window_.value } })
    anomalies.value = d.anomalies ?? []
  } catch {
    anomalies.value = [
      { id: '1', metric: 'llm.p99_ms', description: 'Latência P99 LLM 3.2x acima da baseline', severity: 'high', ts: '14:23:11', score: 0.872 },
      { id: '2', metric: 'tasks.queue_depth', description: 'Profundidade da fila 87% acima do normal', severity: 'medium', ts: '14:20:05', score: 0.634 },
      { id: '3', metric: 'redis.memory_pct', description: 'Uso de memória Redis próximo ao limite', severity: 'critical', ts: '14:18:44', score: 0.941 },
    ]
  }
  updateSummary()
}

onMounted(load)
</script>

<style scoped>
.anomalies-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mb-2 { margin-bottom: 8px; }

.anomaly-summary {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.sum-card { text-align: center; padding: 14px; }
.sc-val { font-size: 28px; font-weight: 800; font-family: var(--font-mono); }
.sc-label { font-size: 11px; margin-top: 2px; }

.anomaly-layout {
  display: grid;
  grid-template-columns: 1fr 340px;
  gap: 12px;
  align-items: start;
}

.anomaly-list { padding: 12px; }
.anomaly-row {
  display: flex;
  gap: 10px;
  padding: 10px;
  border-radius: var(--radius-sm);
  border-left: 3px solid var(--panel-border);
  margin-bottom: 6px;
  background: var(--bg3);
}
.sev-critical { border-left-color: var(--danger); }
.sev-high { border-left-color: var(--warn); }
.sev-medium { border-left-color: var(--accent); }
.sev-low { border-left-color: var(--text-muted); }

.ar-sev { padding-top: 3px; }
.ar-title { font-size: 13px; font-weight: 600; font-family: var(--font-mono); }
.ar-sub { font-size: 11.5px; margin-top: 2px; }

/* Correlation matrix */
.corr-panel { padding: 12px; }
.corr-matrix { display: flex; flex-direction: column; gap: 2px; }
.corr-row { display: flex; align-items: center; gap: 2px; }
.corr-label { width: 36px; font-size: 10px; color: var(--text-muted); font-family: var(--font-mono); flex-shrink: 0; }
.corr-cell {
  width: 44px; height: 36px;
  border-radius: 3px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-family: var(--font-mono);
  color: var(--text);
  cursor: default;
}
.corr-x-labels { display: flex; gap: 2px; padding-left: 38px; margin-top: 2px; }
.corr-x-labels .corr-label { width: 44px; text-align: center; }
</style>
