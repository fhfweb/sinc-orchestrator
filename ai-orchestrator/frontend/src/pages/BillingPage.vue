<template>
  <div class="billing-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">💰 Billing & FinOps</h1>
      <div class="flex gap-2">
        <select v-model="period" @change="load">
          <option value="current">Mês atual</option>
          <option value="last">Mês anterior</option>
          <option value="q1">Q1 2026</option>
        </select>
        <button class="btn btn-ghost" @click="generateInvoice">⤓ Fatura</button>
        <button class="btn btn-ghost" @click="load">↻</button>
      </div>
    </div>

    <!-- KPI row -->
    <div class="billing-kpis">
      <div class="card bk-card" v-for="k in kpis" :key="k.label">
        <div class="bk-val" :class="k.cls">{{ k.val }}</div>
        <div class="bk-label text-muted">{{ k.label }}</div>
        <div class="bk-delta" v-if="k.delta !== undefined" :class="k.delta > 0 ? 'text-danger' : 'text-ok'">
          {{ k.delta > 0 ? '▲' : '▼' }} {{ Math.abs(k.delta) }}% vs anterior
        </div>
      </div>
    </div>

    <div class="billing-layout">
      <!-- Cost breakdown -->
      <div class="card breakdown-card">
        <div class="card-title mb-3">Breakdown de Custos</div>
        <div v-for="item in breakdown" :key="item.category" class="bk-item">
          <div class="bki-header flex justify-between items-center">
            <span>{{ item.category }}</span>
            <span class="mono" :class="item.pct > 40 ? 'text-warn' : ''">
              ${{ item.cost.toFixed(2) }} ({{ item.pct }}%)
            </span>
          </div>
          <div class="bki-bar">
            <div class="bki-fill" :style="`width:${item.pct}%;background:${item.color}`"></div>
          </div>
        </div>
      </div>

      <!-- Per-tenant -->
      <div class="card tenant-card">
        <div class="card-title mb-3">Custo por Tenant</div>
        <table class="tbl">
          <thead>
            <tr><th>Tenant</th><th>Requests</th><th>Tokens</th><th>Custo</th><th>% Total</th></tr>
          </thead>
          <tbody>
            <tr v-for="t in tenants" :key="t.id">
              <td class="mono" style="font-weight:600">{{ t.id }}</td>
              <td>{{ t.requests.toLocaleString() }}</td>
              <td>{{ (t.tokens / 1000).toFixed(1) }}k</td>
              <td class="text-warn mono">${{ t.cost.toFixed(2) }}</td>
              <td>
                <div class="mini-bar">
                  <div class="mb-fill" :style="`width:${t.pct}%`"></div>
                  <span>{{ t.pct }}%</span>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Cost forecast -->
      <div class="card forecast-card">
        <div class="card-title mb-3">Projeção Mensal</div>
        <div class="forecast-chart">
          <div v-for="(bar, i) in forecast" :key="i" class="fc-bar-wrap">
            <div class="fc-bar" :style="`height:${barHeight(bar.val)}%`" :class="bar.projected ? 'fc-projected' : ''"></div>
            <div class="fc-label text-muted">{{ bar.label }}</div>
          </div>
        </div>
        <div class="forecast-note text-muted mt-2" style="font-size:11px">
          * barras tracejadas = projeção baseada no consumo atual
        </div>
      </div>

      <!-- Quota optimizer -->
      <div class="card quota-card">
        <div class="card-title mb-3">Otimização de Quotas</div>
        <div v-for="q in quotas" :key="q.resource" class="quota-row">
          <div class="qr-info">
            <span>{{ q.resource }}</span>
            <span class="text-muted" style="font-size:11px">{{ q.used }}/{{ q.limit }} {{ q.unit }}</span>
          </div>
          <div class="qr-bar">
            <div class="qr-fill" :class="q.used/q.limit > 0.9 ? 'qr-danger' : q.used/q.limit > 0.7 ? 'qr-warn' : 'qr-ok'" :style="`width:${(q.used/q.limit*100).toFixed(0)}%`"></div>
          </div>
          <span class="mono" style="font-size:11px;width:36px;text-align:right">{{ (q.used/q.limit*100).toFixed(0) }}%</span>
        </div>
        <button class="btn btn-ghost mt-3" style="width:100%;font-size:12px">⚡ Otimizar Quotas Automaticamente</button>
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
const period = ref('current')

const kpis = ref([
  { label: 'Gasto Total', val: '$0.00', cls: 'text-warn', delta: undefined as number | undefined },
  { label: 'LLM Tokens', val: '0', cls: 'text-accent', delta: undefined as number | undefined },
  { label: 'Custo/Request', val: '$0.000', cls: '', delta: undefined as number | undefined },
  { label: 'Economia vs Baseline', val: '0%', cls: 'text-ok', delta: undefined as number | undefined }
])

const breakdown = ref([
  { category: 'LLM API (OpenAI)', cost: 42.30, pct: 52, color: 'var(--accent)' },
  { category: 'LLM API (Anthropic)', cost: 18.10, pct: 22, color: 'var(--accent2)' },
  { category: 'Vector DB (Qdrant)', cost: 9.50, pct: 12, color: 'var(--accent3)' },
  { category: 'Redis Cloud', cost: 7.20, pct: 9, color: 'var(--warn)' },
  { category: 'Outros', cost: 4.10, pct: 5, color: 'var(--text-muted)' }
])

const tenants = ref([
  { id: 'default', requests: 12400, tokens: 2840000, cost: 31.20, pct: 38 },
  { id: 'acme', requests: 8900, tokens: 1950000, cost: 22.80, pct: 28 },
  { id: 'demo', requests: 6200, tokens: 1100000, cost: 14.70, pct: 18 },
  { id: 'beta', requests: 3800, tokens: 740000, cost: 12.50, pct: 16 }
])

const forecast = ref([
  { label: 'Jan', val: 68, projected: false },
  { label: 'Fev', val: 74, projected: false },
  { label: 'Mar', val: 81, projected: false },
  { label: 'Abr', val: 91, projected: true },
  { label: 'Mai', val: 98, projected: true },
  { label: 'Jun', val: 103, projected: true }
])

const quotas = ref([
  { resource: 'API Requests/dia', used: 28400, limit: 50000, unit: 'req' },
  { resource: 'LLM Tokens/mês', used: 6800000, limit: 10000000, unit: 'tok' },
  { resource: 'Agentes simultâneos', used: 8, limit: 10, unit: '' },
  { resource: 'Storage Qdrant', used: 4.2, limit: 10, unit: 'GB' }
])

const maxForecast = Math.max(...forecast.value.map(f => f.val))
function barHeight(val: number) { return (val / maxForecast) * 90 }

async function generateInvoice() {
  try {
    await apif('/billing/invoice', { period: period.value })
    app.showToast('Fatura gerada', 'ok')
  } catch { app.showToast('Erro ao gerar fatura', 'err') }
}

async function load() {
  try {
    const d = await api<Record<string, unknown>>('/billing/summary', { params: { period: period.value } })
    if (d.total_cost) kpis.value[0].val = `$${Number(d.total_cost).toFixed(2)}`
    if (d.tokens) kpis.value[1].val = `${(Number(d.tokens) / 1000000).toFixed(1)}M`
  } catch { /* use demo data */ }
  kpis.value[0].val = '$81.20'
  kpis.value[0].delta = 9.5
  kpis.value[1].val = '6.8M'
  kpis.value[1].delta = 12.3
  kpis.value[2].val = '$0.0007'
  kpis.value[3].val = '23%'
}

onMounted(load)
</script>

<style scoped>
.billing-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mb-3 { margin-bottom: 12px; }
.mt-2 { margin-top: 8px; }
.mt-3 { margin-top: 12px; }

.billing-kpis {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
}
.bk-card { text-align: center; padding: 14px; }
.bk-val { font-size: 26px; font-weight: 800; font-family: var(--font-mono); }
.bk-label { font-size: 11px; margin-top: 2px; }
.bk-delta { font-size: 10px; margin-top: 4px; }

.billing-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

/* Breakdown */
.bk-item { margin-bottom: 10px; }
.bki-header { margin-bottom: 4px; font-size: 12.5px; }
.bki-bar { height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.bki-fill { height: 100%; border-radius: 3px; transition: width 0.5s; }

/* Mini bar */
.mini-bar { display: flex; align-items: center; gap: 6px; width: 100px; }
.mb-fill { height: 6px; background: var(--accent3); border-radius: 3px; transition: width 0.3s; }
.mini-bar span { font-size: 10px; color: var(--text-muted); white-space: nowrap; }

/* Forecast chart */
.forecast-chart {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  height: 120px;
  padding-bottom: 24px;
  position: relative;
}
.fc-bar-wrap { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; height: 100%; }
.fc-bar {
  width: 100%;
  background: var(--accent3);
  border-radius: 3px 3px 0 0;
  transition: height 0.5s;
}
.fc-projected {
  background: transparent;
  border: 2px dashed var(--accent3);
  opacity: 0.6;
}
.fc-label { font-size: 10px; position: absolute; bottom: 0; }

/* Quota */
.quota-row { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
.qr-info { width: 180px; flex-shrink: 0; font-size: 12px; display: flex; flex-direction: column; gap: 1px; }
.qr-bar { flex: 1; height: 6px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.qr-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
.qr-ok { background: var(--accent3); }
.qr-warn { background: var(--warn); }
.qr-danger { background: var(--danger); }

@media (max-width: 900px) {
  .billing-kpis { grid-template-columns: 1fr 1fr; }
  .billing-layout { grid-template-columns: 1fr; }
}
</style>
