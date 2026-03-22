<template>
  <div class="bluegreen-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Blue / Green Deployment</h1>
        <p class="text-muted">Manage traffic routing between active and standby environments</p>
      </div>
      <button class="btn btn-ghost" @click="loadData" :disabled="loading">
        {{ loading ? '...' : 'Refresh' }}
      </button>
    </div>

    <!-- Environment Cards -->
    <div class="env-row flex gap-2" style="margin-bottom:1.5rem;">
      <div class="card env-card env-blue" :class="{ 'env-active': deployment.active === 'blue' }">
        <div class="env-badge-row flex items-center justify-between">
          <span class="env-label blue-label">BLUE</span>
          <div class="flex gap-2 items-center">
            <span v-if="deployment.active === 'blue'" class="badge badge-ok">ACTIVE</span>
            <span v-else class="badge badge-info">STANDBY</span>
          </div>
        </div>
        <div class="env-version mono">v{{ deployment.blue.version }}</div>
        <div class="env-traffic">
          <div class="traffic-label text-muted">Traffic</div>
          <div class="traffic-value mono" :class="trafficClass(deployment.blue.traffic)">
            {{ deployment.blue.traffic }}%
          </div>
          <div class="traffic-bar-wrap">
            <div class="traffic-bar-fill blue-fill" :style="{ width: deployment.blue.traffic + '%' }"></div>
          </div>
        </div>
        <div class="env-health-list">
          <div v-for="h in deployment.blue.health" :key="h.name" class="health-row flex items-center justify-between">
            <span class="text-muted" style="font-size:.78rem;">{{ h.name }}</span>
            <span class="badge" :class="h.ok ? 'badge-ok' : 'badge-err'">{{ h.ok ? 'ok' : 'fail' }}</span>
          </div>
        </div>
        <div class="env-deployed text-muted" style="font-size:.75rem; margin-top:.5rem;">
          Deployed: {{ deployment.blue.deployedAt }}
        </div>
      </div>

      <!-- Switch Column -->
      <div class="switch-col flex-col flex items-center justify-center gap-2">
        <div class="traffic-vis text-muted" style="font-size:.75rem; text-align:center;">
          Traffic Split<br>
          <span class="mono text-accent">{{ deployment.blue.traffic }}% / {{ deployment.green.traffic }}%</span>
        </div>
        <div class="arrows">⇄</div>
        <button class="btn btn-primary" @click="confirmSwitch = true" :disabled="switching">
          Switch Traffic
        </button>
      </div>

      <div class="card env-card env-green" :class="{ 'env-active': deployment.active === 'green' }">
        <div class="env-badge-row flex items-center justify-between">
          <span class="env-label green-label">GREEN</span>
          <div class="flex gap-2 items-center">
            <span v-if="deployment.active === 'green'" class="badge badge-ok">ACTIVE</span>
            <span v-else class="badge badge-info">STANDBY</span>
          </div>
        </div>
        <div class="env-version mono">v{{ deployment.green.version }}</div>
        <div class="env-traffic">
          <div class="traffic-label text-muted">Traffic</div>
          <div class="traffic-value mono" :class="trafficClass(deployment.green.traffic)">
            {{ deployment.green.traffic }}%
          </div>
          <div class="traffic-bar-wrap">
            <div class="traffic-bar-fill green-fill" :style="{ width: deployment.green.traffic + '%' }"></div>
          </div>
        </div>
        <div class="env-health-list">
          <div v-for="h in deployment.green.health" :key="h.name" class="health-row flex items-center justify-between">
            <span class="text-muted" style="font-size:.78rem;">{{ h.name }}</span>
            <span class="badge" :class="h.ok ? 'badge-ok' : 'badge-err'">{{ h.ok ? 'ok' : 'fail' }}</span>
          </div>
        </div>
        <div class="env-deployed text-muted" style="font-size:.75rem; margin-top:.5rem;">
          Deployed: {{ deployment.green.deployedAt }}
        </div>
      </div>
    </div>

    <!-- Health Comparison Table -->
    <div class="card">
      <div class="section-title">Health Comparison</div>
      <table class="tbl">
        <thead>
          <tr>
            <th>Check</th>
            <th>Blue</th>
            <th>Green</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(h, i) in deployment.blue.health" :key="h.name">
            <td style="font-weight:600; font-size:.88rem;">{{ h.name }}</td>
            <td><span class="badge" :class="h.ok ? 'badge-ok' : 'badge-err'">{{ h.ok ? 'pass' : 'fail' }}</span></td>
            <td><span class="badge" :class="deployment.green.health[i]?.ok ? 'badge-ok' : 'badge-err'">
              {{ deployment.green.health[i]?.ok ? 'pass' : 'fail' }}
            </span></td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Confirm Modal -->
    <div v-if="confirmSwitch" class="modal-backdrop" @click.self="confirmSwitch = false">
      <div class="card modal-box">
        <h2 class="modal-title text-warn">Confirm Traffic Switch</h2>
        <p style="font-size:.9rem; color:var(--text-muted); margin-bottom:1rem;">
          This will route 100% of traffic from
          <strong class="text-accent">{{ deployment.active === 'blue' ? 'Blue' : 'Green' }}</strong>
          to <strong class="text-accent">{{ deployment.active === 'blue' ? 'Green' : 'Blue' }}</strong>.
          This action is reversible.
        </p>
        <div class="flex gap-2 justify-between">
          <button class="btn btn-ghost" @click="confirmSwitch = false">Cancel</button>
          <button class="btn btn-primary" @click="switchTraffic" :disabled="switching">
            {{ switching ? 'Switching...' : 'Confirm Switch' }}
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const store = useAppStore()

interface HealthCheck { name: string; ok: boolean }
interface Env { version: string; traffic: number; health: HealthCheck[]; deployedAt: string }
interface Deployment { active: 'blue' | 'green'; blue: Env; green: Env }

const loading = ref(false)
const switching = ref(false)
const confirmSwitch = ref(false)

const healthChecks: HealthCheck[] = [
  { name: 'API Gateway', ok: true },
  { name: 'Database Connectivity', ok: true },
  { name: 'Redis Cache', ok: true },
  { name: 'LLM Router', ok: true },
  { name: 'Memory Service', ok: true },
]
const greenHealthChecks: HealthCheck[] = [
  { name: 'API Gateway', ok: true },
  { name: 'Database Connectivity', ok: true },
  { name: 'Redis Cache', ok: true },
  { name: 'LLM Router', ok: false },
  { name: 'Memory Service', ok: true },
]

const deployment = ref<Deployment>({
  active: 'blue',
  blue: { version: '2.4.1', traffic: 100, health: healthChecks, deployedAt: '2026-03-20 09:00 UTC' },
  green: { version: '2.5.0-rc1', traffic: 0, health: greenHealthChecks, deployedAt: '2026-03-22 13:00 UTC' }
})

function trafficClass(pct: number) {
  if (pct === 100) return 'text-ok'
  if (pct === 0) return 'text-muted'
  return 'text-warn'
}

async function switchTraffic() {
  switching.value = true
  confirmSwitch.value = false
  try {
    const res = await api<any>('/deployments/blue-green/switch', { method: 'POST' })
    deployment.value = res
    store.showToast('Traffic switched successfully', 'ok')
  } catch {
    // Demo: toggle locally
    const was = deployment.value.active
    deployment.value.active = was === 'blue' ? 'green' : 'blue'
    deployment.value.blue.traffic = was === 'blue' ? 0 : 100
    deployment.value.green.traffic = was === 'blue' ? 100 : 0
    store.showToast('Switched (demo mode)', 'ok')
  } finally {
    switching.value = false
  }
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/deployments/blue-green/status')
    deployment.value = res
  } catch {
    // keep demo data
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.bluegreen-page { padding: 1.5rem; max-width: 1200px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.env-row { align-items: stretch; }
.env-card { flex: 1; padding: 1.25rem; }
.env-active { box-shadow: 0 0 0 2px var(--accent), 0 0 20px color-mix(in srgb, var(--accent) 20%, transparent); }

.env-label { font-size: .7rem; font-weight: 900; letter-spacing: .1em; }
.blue-label { color: #60a5fa; }
.green-label { color: var(--accent3); }
.env-version { font-size: 1.5rem; font-weight: 700; margin: .5rem 0; }
.env-badge-row { margin-bottom: .5rem; }

.env-traffic { margin: .75rem 0; }
.traffic-label { font-size: .7rem; letter-spacing: .06em; margin-bottom: .2rem; }
.traffic-value { font-size: 1.8rem; font-weight: 700; margin-bottom: .25rem; }
.traffic-bar-wrap { height: 6px; background: var(--bg3); border-radius: 3px; }
.traffic-bar-fill { height: 100%; border-radius: 3px; transition: width .6s ease; }
.blue-fill { background: #60a5fa; }
.green-fill { background: var(--accent3); }

.env-health-list { display: flex; flex-direction: column; gap: .3rem; margin-top: .75rem; padding-top: .75rem; border-top: 1px solid var(--panel-border); }
.health-row { }

.switch-col { flex-shrink: 0; width: 140px; gap: .75rem; }
.traffic-vis { line-height: 1.5; }
.arrows { font-size: 2rem; color: var(--accent); }

.section-title { font-weight: 700; font-size: .9rem; padding: .75rem 1rem; border-bottom: 1px solid var(--panel-border); }

.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
.modal-box { padding: 1.5rem; min-width: 380px; max-width: 460px; width: 100%; }
.modal-title { font-size: 1.1rem; font-weight: 700; margin: 0 0 .75rem; }
.justify-between { justify-content: space-between; }
</style>
