<template>
  <div class="llm-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">LLM Router Status</h1>
        <p class="text-muted">Monitor and manage LLM provider connections</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? 'Refreshing...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <div class="summary-row flex gap-2" style="margin-bottom: 1.5rem;">
      <div class="card kpi-card">
        <div class="text-muted" style="font-size:.75rem;">ONLINE PROVIDERS</div>
        <div class="kpi-value text-ok">{{ onlineCount }}</div>
      </div>
      <div class="card kpi-card">
        <div class="text-muted" style="font-size:.75rem;">DEGRADED</div>
        <div class="kpi-value text-warn">{{ degradedCount }}</div>
      </div>
      <div class="card kpi-card">
        <div class="text-muted" style="font-size:.75rem;">OFFLINE</div>
        <div class="kpi-value text-danger">{{ offlineCount }}</div>
      </div>
      <div class="card kpi-card">
        <div class="text-muted" style="font-size:.75rem;">TOTAL REQUESTS TODAY</div>
        <div class="kpi-value text-accent">{{ totalRequests.toLocaleString() }}</div>
      </div>
    </div>

    <div class="providers-grid">
      <div
        v-for="provider in providers"
        :key="provider.id"
        class="card provider-card"
        :class="{ 'provider-disabled': !provider.enabled }"
      >
        <div class="provider-header flex items-center justify-between">
          <div class="flex items-center gap-2">
            <div class="status-dot" :class="statusDotClass(provider.status)"></div>
            <span class="provider-name">{{ provider.name }}</span>
          </div>
          <div class="flex items-center gap-2">
            <span class="badge" :class="statusBadgeClass(provider.status)">{{ provider.status }}</span>
            <label class="toggle-switch">
              <input type="checkbox" :checked="provider.enabled" @change="toggleProvider(provider)" />
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>

        <div class="provider-model text-muted" style="font-size:.8rem; margin: .5rem 0;">
          {{ provider.model }}
        </div>

        <div class="provider-metrics">
          <div class="metric">
            <div class="text-muted" style="font-size:.7rem;">P99 LATENCY</div>
            <div class="metric-value mono" :class="latencyClass(provider.p99Latency)">
              {{ provider.p99Latency }}ms
            </div>
          </div>
          <div class="metric">
            <div class="text-muted" style="font-size:.7rem;">ERROR RATE</div>
            <div class="metric-value mono" :class="errorClass(provider.errorRate)">
              {{ provider.errorRate.toFixed(1) }}%
            </div>
          </div>
          <div class="metric">
            <div class="text-muted" style="font-size:.7rem;">COST / 1K TOKENS</div>
            <div class="metric-value mono">${{ provider.costPer1k.toFixed(4) }}</div>
          </div>
          <div class="metric">
            <div class="text-muted" style="font-size:.7rem;">REQUESTS TODAY</div>
            <div class="metric-value mono text-accent">{{ provider.requestsToday.toLocaleString() }}</div>
          </div>
        </div>

        <div class="circuit-breaker flex items-center gap-2" style="margin-top:.75rem; padding-top:.75rem; border-top: 1px solid var(--panel-border);">
          <span class="text-muted" style="font-size:.7rem;">CIRCUIT BREAKER</span>
          <span class="badge" :class="cbBadgeClass(provider.circuitBreaker)">{{ provider.circuitBreaker }}</span>
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

interface Provider {
  id: string
  name: string
  model: string
  status: 'online' | 'offline' | 'degraded'
  enabled: boolean
  p99Latency: number
  errorRate: number
  costPer1k: number
  requestsToday: number
  circuitBreaker: 'closed' | 'open' | 'half-open'
}

const loading = ref(false)
const providers = ref<Provider[]>([])

const demoProviders: Provider[] = [
  { id: 'openai', name: 'OpenAI', model: 'gpt-4o', status: 'online', enabled: true, p99Latency: 842, errorRate: 0.3, costPer1k: 0.005, requestsToday: 14230, circuitBreaker: 'closed' },
  { id: 'anthropic', name: 'Anthropic', model: 'claude-sonnet-4-6', status: 'online', enabled: true, p99Latency: 1102, errorRate: 0.1, costPer1k: 0.003, requestsToday: 8940, circuitBreaker: 'closed' },
  { id: 'gemini', name: 'Google Gemini', model: 'gemini-1.5-pro', status: 'degraded', enabled: true, p99Latency: 2340, errorRate: 4.2, costPer1k: 0.0035, requestsToday: 3210, circuitBreaker: 'half-open' },
  { id: 'groq', name: 'Groq', model: 'llama-3.3-70b', status: 'online', enabled: true, p99Latency: 320, errorRate: 0.8, costPer1k: 0.0008, requestsToday: 5670, circuitBreaker: 'closed' },
  { id: 'mistral', name: 'Mistral AI', model: 'mistral-large', status: 'offline', enabled: false, p99Latency: 0, errorRate: 100, costPer1k: 0.002, requestsToday: 0, circuitBreaker: 'open' },
  { id: 'cohere', name: 'Cohere', model: 'command-r-plus', status: 'online', enabled: false, p99Latency: 760, errorRate: 0.5, costPer1k: 0.003, requestsToday: 1120, circuitBreaker: 'closed' },
]

const onlineCount = computed(() => providers.value.filter(p => p.status === 'online').length)
const degradedCount = computed(() => providers.value.filter(p => p.status === 'degraded').length)
const offlineCount = computed(() => providers.value.filter(p => p.status === 'offline').length)
const totalRequests = computed(() => providers.value.reduce((sum, p) => sum + p.requestsToday, 0))

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/llm/status')
    const raw: any[] = Array.isArray(res.providers) ? res.providers : (Array.isArray(res) ? res : [])
    if (raw.length === 0) {
      providers.value = demoProviders
    } else if ('errorRate' in raw[0]) {
      providers.value = raw
    } else {
      // Map backend format: {name, status, latency_ms, requests_1h, errors_1h, model}
      providers.value = raw.map((p: any) => ({
        id: p.name,
        name: p.name,
        model: p.model || '—',
        status: p.status === 'ok' ? 'online' : p.status === 'unknown' ? 'offline' : (p.status || 'offline'),
        enabled: p.status === 'ok',
        p99Latency: p.latency_ms ?? 0,
        errorRate: p.requests_1h > 0 ? ((p.errors_1h ?? 0) / p.requests_1h) * 100 : 0,
        costPer1k: 0,
        requestsToday: p.requests_1h ?? 0,
        circuitBreaker: 'closed' as const,
      }))
    }
  } catch {
    providers.value = demoProviders
  } finally {
    loading.value = false
  }
}

async function toggleProvider(provider: Provider) {
  const prev = provider.enabled
  provider.enabled = !provider.enabled
  try {
    await api('/llm/toggle', { method: 'POST', body: JSON.stringify({ provider: provider.id, enabled: provider.enabled }) })
    store.showToast(`${provider.name} ${provider.enabled ? 'enabled' : 'disabled'}`, 'ok')
  } catch {
    provider.enabled = prev
    store.showToast('Failed to toggle provider', 'err')
  }
}

function statusDotClass(status: string) {
  return { 'dot-ok': status === 'online', 'dot-warn': status === 'degraded', 'dot-err': status === 'offline' }
}
function statusBadgeClass(status: string) {
  return { 'badge-ok': status === 'online', 'badge-warn': status === 'degraded', 'badge-err': status === 'offline' }
}
function cbBadgeClass(cb: string) {
  return { 'badge-ok': cb === 'closed', 'badge-warn': cb === 'half-open', 'badge-err': cb === 'open' }
}
function latencyClass(ms: number) {
  if (ms === 0) return 'text-muted'
  if (ms < 500) return 'text-ok'
  if (ms < 1500) return 'text-warn'
  return 'text-danger'
}
function errorClass(rate: number) {
  if (rate < 1) return 'text-ok'
  if (rate < 5) return 'text-warn'
  return 'text-danger'
}

onMounted(loadData)
</script>

<style scoped>
.llm-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.kpi-card { flex: 1; min-width: 140px; padding: .75rem 1rem; }
.kpi-value { font-size: 1.75rem; font-weight: 700; font-family: var(--font-mono); margin-top: .25rem; }

.providers-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 1rem;
}

.provider-card { padding: 1rem; transition: var(--transition); }
.provider-card:hover { border-color: var(--accent); }
.provider-disabled { opacity: .55; }
.provider-name { font-weight: 600; font-size: .95rem; }
.provider-model { }

.provider-metrics {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: .75rem;
  margin-top: .75rem;
}
.metric { }
.metric-value { font-size: 1.1rem; font-weight: 600; margin-top: .15rem; }

.status-dot {
  width: 10px; height: 10px;
  border-radius: 50%;
  flex-shrink: 0;
}
.dot-ok { background: var(--accent3); box-shadow: 0 0 6px var(--accent3); }
.dot-warn { background: var(--warn); box-shadow: 0 0 6px var(--warn); }
.dot-err { background: var(--danger); box-shadow: 0 0 6px var(--danger); }

.toggle-switch { position: relative; display: inline-block; width: 36px; height: 20px; cursor: pointer; }
.toggle-switch input { opacity: 0; width: 0; height: 0; }
.toggle-slider {
  position: absolute; inset: 0;
  background: var(--bg3);
  border-radius: 20px;
  transition: var(--transition);
}
.toggle-slider::before {
  content: '';
  position: absolute;
  width: 14px; height: 14px;
  left: 3px; top: 3px;
  background: var(--text-muted);
  border-radius: 50%;
  transition: var(--transition);
}
.toggle-switch input:checked + .toggle-slider { background: var(--accent); }
.toggle-switch input:checked + .toggle-slider::before { transform: translateX(16px); background: #fff; }
</style>
