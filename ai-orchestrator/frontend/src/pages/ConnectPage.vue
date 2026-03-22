<template>
  <div class="connect-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">External Connections</h1>
        <p class="text-muted">Manage integrations with external services and platforms</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-primary" @click="showAddModal = true">+ Add Integration</button>
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? '...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <div class="integrations-grid">
      <div
        v-for="intg in integrations"
        :key="intg.id"
        class="card integration-card"
      >
        <div class="intg-header flex items-center justify-between">
          <div class="flex items-center gap-2">
            <div class="intg-icon">{{ intg.icon }}</div>
            <div>
              <div class="intg-name">{{ intg.name }}</div>
              <div class="text-muted" style="font-size:.72rem;">{{ intg.type }}</div>
            </div>
          </div>
          <span class="badge" :class="statusBadge(intg.status)">{{ intg.status }}</span>
        </div>

        <div class="intg-webhook flex items-center gap-2" style="margin:.75rem 0;">
          <span class="text-muted" style="font-size:.7rem;">WEBHOOK</span>
          <span class="mono webhook-url truncate">{{ maskUrl(intg.webhookUrl) }}</span>
        </div>

        <div class="intg-sync text-muted" style="font-size:.75rem; margin-bottom:.75rem;">
          Last synced: {{ intg.lastSynced }}
        </div>

        <div class="flex gap-2">
          <button
            class="btn btn-ghost"
            style="flex:1; font-size:.78rem;"
            @click="testConnection(intg)"
            :disabled="intg.testing"
          >
            {{ intg.testing ? 'Testing...' : 'Test' }}
          </button>
          <button class="btn btn-ghost" style="font-size:.78rem;" @click="openEdit(intg)">Edit</button>
        </div>
      </div>
    </div>

    <!-- Add Integration Modal -->
    <div v-if="showAddModal" class="modal-backdrop" @click.self="showAddModal = false">
      <div class="card modal-box">
        <h2 class="modal-title">Add Integration</h2>
        <div class="form-grid">
          <label class="form-label">
            Name
            <input class="form-input" v-model="newIntg.name" placeholder="e.g. My Slack" />
          </label>
          <label class="form-label">
            Type
            <select class="form-input" v-model="newIntg.type">
              <option v-for="t in integrationTypes" :key="t" :value="t">{{ t }}</option>
            </select>
          </label>
          <label class="form-label">
            Webhook URL
            <input class="form-input mono" v-model="newIntg.webhookUrl" placeholder="https://..." />
          </label>
          <label class="form-label">
            API Key / Token
            <input class="form-input mono" type="password" v-model="newIntg.token" placeholder="token..." />
          </label>
        </div>
        <div class="flex gap-2" style="margin-top:1rem; justify-content:flex-end;">
          <button class="btn btn-ghost" @click="showAddModal = false">Cancel</button>
          <button class="btn btn-primary" @click="addIntegration" :disabled="saving">
            {{ saving ? 'Adding...' : 'Add' }}
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

interface Integration {
  id: string
  name: string
  type: string
  icon: string
  status: 'connected' | 'disconnected' | 'err'
  lastSynced: string
  webhookUrl: string
  testing?: boolean
}

const loading = ref(false)
const saving = ref(false)
const showAddModal = ref(false)
const integrations = ref<Integration[]>([])
const newIntg = ref({ name: '', type: 'Slack', webhookUrl: '', token: '' })

const integrationTypes = ['Slack', 'GitHub', 'Jira', 'PagerDuty', 'Datadog', 'Grafana', 'Webhook', 'Email']

const demoIntegrations: Integration[] = [
  { id: 'i1', name: 'Slack Alerts', type: 'Slack', icon: '💬', status: 'connected', lastSynced: '2 min ago', webhookUrl: 'https://hooks.slack.com/services/T0...XXXX' },
  { id: 'i2', name: 'GitHub Actions', type: 'GitHub', icon: '🐙', status: 'connected', lastSynced: '5 min ago', webhookUrl: 'https://github.com/webhooks/...XXXX' },
  { id: 'i3', name: 'Jira Tickets', type: 'Jira', icon: '🎯', status: 'connected', lastSynced: '1 hr ago', webhookUrl: 'https://yourorg.atlassian.net/...XXXX' },
  { id: 'i4', name: 'PagerDuty On-Call', type: 'PagerDuty', icon: '🔔', status: 'connected', lastSynced: '10 min ago', webhookUrl: 'https://events.pagerduty.com/...XXXX' },
  { id: 'i5', name: 'Datadog Metrics', type: 'Datadog', icon: '📊', status: 'disconnected', lastSynced: '2 days ago', webhookUrl: 'https://api.datadoghq.com/...XXXX' },
  { id: 'i6', name: 'Grafana Alerts', type: 'Grafana', icon: '📈', status: 'err', lastSynced: '3 days ago', webhookUrl: 'https://grafana.yourhost.com/...XXXX' },
]

function statusBadge(s: string) {
  if (s === 'connected') return 'badge-ok'
  if (s === 'disconnected') return 'badge-info'
  return 'badge-err'
}

function maskUrl(url: string) {
  if (!url) return '—'
  const idx = url.lastIndexOf('/')
  if (idx < 0) return url.substring(0, 30) + '...'
  return url.substring(0, Math.min(30, idx + 8)) + '...' + url.substring(url.length - 4)
}

async function testConnection(intg: Integration) {
  intg.testing = true
  try {
    await api('/connect/test', { method: 'POST', body: JSON.stringify({ id: intg.id }) })
    store.showToast(`${intg.name}: connection OK`, 'ok')
    intg.status = 'connected'
  } catch {
    store.showToast(`${intg.name}: connection failed`, 'err')
    intg.status = 'err'
  } finally {
    intg.testing = false
    intg.lastSynced = 'just now'
  }
}

function openEdit(intg: Integration) {
  newIntg.value = { name: intg.name, type: intg.type, webhookUrl: intg.webhookUrl, token: '' }
  showAddModal.value = true
}

async function addIntegration() {
  saving.value = true
  try {
    const res = await api<any>('/connect/integrations', {
      method: 'POST',
      body: JSON.stringify(newIntg.value)
    })
    integrations.value.unshift(res.integration ?? {
      id: 'i' + Date.now(),
      name: newIntg.value.name,
      type: newIntg.value.type,
      icon: '🔗',
      status: 'disconnected',
      lastSynced: 'never',
      webhookUrl: newIntg.value.webhookUrl
    })
    store.showToast('Integration added', 'ok')
    showAddModal.value = false
    newIntg.value = { name: '', type: 'Slack', webhookUrl: '', token: '' }
  } catch {
    store.showToast('Failed to add integration', 'err')
  } finally {
    saving.value = false
  }
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/connect/integrations')
    integrations.value = res.integrations ?? res
  } catch {
    integrations.value = demoIntegrations
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.connect-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.integrations-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1rem;
}

.integration-card { padding: 1rem; transition: var(--transition); }
.integration-card:hover { border-color: var(--accent); }

.intg-icon { font-size: 1.5rem; line-height: 1; }
.intg-name { font-weight: 700; font-size: .95rem; }

.webhook-url {
  font-size: .72rem;
  color: var(--text-muted);
  flex: 1;
  min-width: 0;
}

.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
.modal-box { padding: 1.5rem; min-width: 380px; max-width: 480px; width: 100%; }
.modal-title { font-size: 1.1rem; font-weight: 700; margin: 0 0 1rem; }

.form-grid { display: flex; flex-direction: column; gap: .75rem; }
.form-label { display: flex; flex-direction: column; gap: .3rem; font-size: .82rem; color: var(--text-muted); }
.form-input {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .4rem .6rem;
  font-size: .88rem;
  outline: none;
  transition: var(--transition);
}
.form-input:focus { border-color: var(--accent); }
</style>
