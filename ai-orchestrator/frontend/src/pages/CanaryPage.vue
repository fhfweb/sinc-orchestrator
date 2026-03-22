<template>
  <div class="canary-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">🐤 Canary Deployment</h1>
      <div class="flex gap-2">
        <button class="btn btn-ghost" @click="load">↻ Refresh</button>
        <button class="btn btn-primary" @click="openModal">+ Novo Canary</button>
      </div>
    </div>

    <div v-if="canary" class="active-canary card">
      <div class="ac-header flex justify-between items-center">
        <div>
          <div class="ac-title">Canary ativo: <span class="text-accent">{{ canary.version }}</span></div>
          <div class="text-muted" style="font-size:12px">Iniciado {{ canary.started_at }} · Fase {{ canary.phase }}/{{ canary.total_phases }}</div>
        </div>
        <div class="flex gap-2">
          <button class="btn btn-ghost" @click="advance" :disabled="canary.phase >= canary.total_phases">▶ Avançar fase</button>
          <button class="btn btn-danger" @click="rollback">⏮ Rollback</button>
        </div>
      </div>

      <!-- Traffic split -->
      <div class="traffic-split mt-4">
        <div class="ts-label flex justify-between mb-2">
          <span class="text-muted">Stable ({{ 100 - canary.traffic_pct }}%)</span>
          <span class="text-accent">Canary ({{ canary.traffic_pct }}%)</span>
        </div>
        <div class="ts-bar">
          <div class="ts-stable" :style="`width:${100 - canary.traffic_pct}%`"></div>
          <div class="ts-canary" :style="`width:${canary.traffic_pct}%`"></div>
        </div>
      </div>

      <!-- Phase progress -->
      <div class="phases mt-4">
        <div v-for="(p, i) in canary.phases" :key="i" class="phase-item" :class="{ 'phase-done': i < canary.phase, 'phase-active': i === canary.phase - 1 }">
          <div class="phase-dot"></div>
          <div class="phase-info">
            <span class="phase-name">{{ p.name }}</span>
            <span class="phase-traffic text-muted">{{ p.traffic }}% tráfego</span>
          </div>
          <span v-if="i < canary.phase" class="text-ok">✓</span>
          <span v-else-if="i === canary.phase - 1" class="text-accent">▶</span>
        </div>
      </div>

      <!-- Metrics comparison -->
      <div class="canary-metrics mt-4">
        <div class="cm-title card-title mb-2">Métricas: Stable vs Canary</div>
        <div class="metrics-grid">
          <div v-for="m in canary.metrics" :key="m.name" class="metric-card card">
            <div class="mc-name text-muted">{{ m.name }}</div>
            <div class="mc-vals">
              <div class="mc-val">{{ m.stable }}<span class="mc-unit">{{ m.unit }}</span></div>
              <div class="mc-sep">vs</div>
              <div class="mc-val" :class="m.canary_better ? 'text-ok' : 'text-warn'">{{ m.canary }}<span class="mc-unit">{{ m.unit }}</span></div>
            </div>
            <div class="mc-labels flex justify-between text-muted" style="font-size:10px">
              <span>Stable</span><span>Canary</span>
            </div>
          </div>
        </div>
      </div>
    </div>

    <div v-else class="no-canary card">
      <div style="font-size:32px;opacity:0.3">🐤</div>
      <div class="text-muted">Nenhum deploy canary ativo</div>
      <button class="btn btn-primary mt-2" @click="openModal">Iniciar Canary Deploy</button>
    </div>

    <div v-if="modalOpen" class="modal-overlay" @click.self="modalOpen = false">
      <div class="modal-box card">
        <h3 class="modal-title">Novo Canary Deploy</h3>
        <div class="form-grid mt-3">
          <label><span class="form-label">Versão</span><input v-model="form.version" placeholder="v2.1.0" /></label>
          <label><span class="form-label">Fases</span><input v-model.number="form.phases" type="number" min="2" max="10" /></label>
          <label><span class="form-label">Tráfego inicial</span>
            <div class="flex items-center gap-2">
              <input v-model.number="form.initial_pct" type="range" min="1" max="30" />
              <span class="mono" style="width:36px">{{ form.initial_pct }}%</span>
            </div>
          </label>
        </div>
        <div class="flex gap-2 justify-end mt-4">
          <button class="btn btn-ghost" @click="modalOpen = false">Cancelar</button>
          <button class="btn btn-primary" @click="startCanary">Iniciar</button>
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

interface CanaryPhase { name: string; traffic: number }
interface CanaryMetric { name: string; unit: string; stable: number; canary: number; canary_better: boolean }
interface Canary { version: string; started_at: string; phase: number; total_phases: number; traffic_pct: number; phases: CanaryPhase[]; metrics: CanaryMetric[] }

const canary = ref<Canary | null>(null)
const modalOpen = ref(false)
const form = ref({ version: '', phases: 4, initial_pct: 5 })

const DEMO: Canary = {
  version: 'v2.1.0', started_at: '2026-03-22 09:00', phase: 2, total_phases: 4, traffic_pct: 25,
  phases: [
    { name: 'Fase 1 — Smoke test', traffic: 5 },
    { name: 'Fase 2 — Canary 25%', traffic: 25 },
    { name: 'Fase 3 — Canary 50%', traffic: 50 },
    { name: 'Fase 4 — Full rollout', traffic: 100 },
  ],
  metrics: [
    { name: 'Erro rate', unit: '%', stable: 0.2, canary: 0.1, canary_better: true },
    { name: 'P99 latência', unit: 'ms', stable: 340, canary: 290, canary_better: true },
    { name: 'Throughput', unit: 'r/s', stable: 42, canary: 44, canary_better: true },
    { name: 'CPU', unit: '%', stable: 38, canary: 41, canary_better: false },
  ]
}

function openModal() { modalOpen.value = true }

async function advance() {
  try {
    await apif('/deployments/canary/advance', {})
    app.showToast('Canary avançado', 'ok')
    load()
  } catch { canary.value && canary.value.phase++ ; app.showToast('Fase avançada (demo)', 'info') }
}

async function rollback() {
  if (!confirm('Fazer rollback do canary?')) return
  try {
    await apif('/deployments/canary/rollback', {})
    canary.value = null
    app.showToast('Canary revertido', 'ok')
  } catch { app.showToast('Erro no rollback', 'err') }
}

async function startCanary() {
  try {
    await apif('/deployments/canary/start', form.value)
    app.showToast('Canary iniciado', 'ok')
    modalOpen.value = false
    load()
  } catch { canary.value = DEMO; modalOpen.value = false; app.showToast('Canary iniciado (demo)', 'info') }
}

async function load() {
  try {
    const d = await api<{ canary: Canary | null }>('/deployments/canary/status')
    canary.value = d.canary
  } catch { canary.value = DEMO }
}

onMounted(load)
</script>

<style scoped>
.canary-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mt-2 { margin-top: 8px; }
.mt-4 { margin-top: 16px; }
.mb-2 { margin-bottom: 8px; }

.active-canary { padding: 20px; }
.ac-title { font-size: 15px; font-weight: 700; }

.ts-bar { display: flex; height: 24px; border-radius: 12px; overflow: hidden; }
.ts-stable { background: var(--text-muted); opacity: 0.4; transition: width 0.5s; }
.ts-canary { background: var(--accent); transition: width 0.5s; }

.phases { display: flex; flex-direction: column; gap: 8px; }
.phase-item { display: flex; align-items: center; gap: 12px; padding: 8px; border-radius: var(--radius-sm); }
.phase-done { color: var(--accent3); }
.phase-active { background: rgba(0,212,255,0.06); border: 1px solid rgba(0,212,255,0.2); }
.phase-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; }
.phase-done .phase-dot { background: var(--accent3); }
.phase-active .phase-dot { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.phase-info { flex: 1; display: flex; justify-content: space-between; font-size: 12px; }
.phase-traffic { font-size: 11px; }

.metrics-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.metric-card { padding: 12px; text-align: center; }
.mc-name { font-size: 11px; margin-bottom: 8px; }
.mc-vals { display: flex; justify-content: center; align-items: center; gap: 8px; font-family: var(--font-mono); font-size: 18px; font-weight: 700; }
.mc-sep { font-size: 11px; color: var(--text-muted); }
.mc-unit { font-size: 11px; color: var(--text-muted); }

.no-canary { display: flex; flex-direction: column; align-items: center; justify-content: center; min-height: 300px; gap: 12px; }

.form-grid { display: flex; flex-direction: column; gap: 10px; }
.form-label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 500; backdrop-filter: blur(4px); }
.modal-box { width: 420px; padding: 20px; }
.modal-title { font-size: 15px; font-weight: 700; }

@media (max-width: 900px) { .metrics-grid { grid-template-columns: repeat(2, 1fr); } }
</style>
