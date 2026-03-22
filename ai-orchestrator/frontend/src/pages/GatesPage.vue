<template>
  <div class="gates-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Governance Gates</h1>
        <p class="text-muted">Configure and monitor rules that control agent and API behavior</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-primary" @click="showAddModal = true">+ Add Gate</button>
        <button class="btn btn-ghost" @click="loadData" :disabled="loading">
          {{ loading ? '...' : 'Refresh' }}
        </button>
      </div>
    </div>

    <!-- Summary -->
    <div class="flex gap-2" style="margin-bottom:1.5rem; flex-wrap:wrap;">
      <div class="card kpi-card">
        <div class="kpi-label text-muted">ACTIVE GATES</div>
        <div class="kpi-value text-ok">{{ activeCount }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">INACTIVE</div>
        <div class="kpi-value text-muted">{{ inactiveCount }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">TRIGGERED TODAY</div>
        <div class="kpi-value text-warn">{{ totalTriggeredToday }}</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-label text-muted">BLOCKED TODAY</div>
        <div class="kpi-value text-danger">{{ totalBlocked }}</div>
      </div>
    </div>

    <!-- Gates Table -->
    <div class="card" style="margin-bottom:1.5rem; overflow-x:auto;">
      <div class="section-title">Governance Rules</div>
      <table class="tbl">
        <thead>
          <tr>
            <th>Gate Name</th>
            <th>Condition</th>
            <th>Action</th>
            <th>Triggered</th>
            <th>Status</th>
            <th>Toggle</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="gate in gates" :key="gate.id" :class="{ 'inactive-row': !gate.active }">
            <td>
              <div style="font-weight:600; font-size:.9rem;">{{ gate.name }}</div>
              <div class="text-muted" style="font-size:.72rem;">{{ gate.description }}</div>
            </td>
            <td class="mono text-muted" style="font-size:.78rem; max-width:240px;">{{ gate.condition }}</td>
            <td>
              <span class="badge" :class="actionBadge(gate.action)">{{ gate.action }}</span>
            </td>
            <td class="mono" :class="gate.triggeredCount > 0 ? 'text-warn' : 'text-muted'">
              {{ gate.triggeredCount.toLocaleString() }}
            </td>
            <td>
              <span class="badge" :class="gate.active ? 'badge-ok' : 'badge-info'">
                {{ gate.active ? 'active' : 'inactive' }}
              </span>
            </td>
            <td>
              <label class="toggle-switch">
                <input type="checkbox" :checked="gate.active" @change="toggleGate(gate)" />
                <span class="toggle-slider"></span>
              </label>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Triggered Events Log -->
    <div class="card">
      <div class="section-title flex items-center justify-between">
        <span>Triggered Events Log</span>
        <span class="badge badge-info">last {{ events.length }}</span>
      </div>
      <table class="tbl">
        <thead>
          <tr>
            <th>Time</th>
            <th>Gate</th>
            <th>Action Taken</th>
            <th>Context</th>
            <th>Tenant</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="evt in events" :key="evt.id">
            <td class="mono text-muted" style="font-size:.75rem;">{{ evt.time }}</td>
            <td style="font-size:.85rem; font-weight:600;">{{ evt.gate }}</td>
            <td><span class="badge" :class="actionBadge(evt.action)">{{ evt.action }}</span></td>
            <td class="text-muted truncate" style="font-size:.8rem; max-width:280px;">{{ evt.context }}</td>
            <td class="mono text-muted" style="font-size:.75rem;">{{ evt.tenant }}</td>
          </tr>
          <tr v-if="!events.length">
            <td colspan="5" class="text-muted" style="text-align:center; padding:1.5rem;">No triggered events</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Add Gate Modal -->
    <div v-if="showAddModal" class="modal-backdrop" @click.self="showAddModal = false">
      <div class="card modal-box">
        <h2 class="modal-title">Add Governance Gate</h2>
        <div class="form-grid">
          <label class="form-label">
            Name
            <input class="form-input" v-model="newGate.name" placeholder="Token Limit Guard" />
          </label>
          <label class="form-label">
            Condition
            <input class="form-input mono" v-model="newGate.condition" placeholder="token_usage > 0.9 * daily_limit" />
          </label>
          <label class="form-label">
            Action
            <select class="form-input" v-model="newGate.action">
              <option>block</option>
              <option>warn</option>
              <option>log</option>
            </select>
          </label>
          <label class="form-label">
            Description
            <input class="form-input" v-model="newGate.description" placeholder="Short description" />
          </label>
        </div>
        <div class="flex gap-2" style="margin-top:1rem; justify-content:flex-end;">
          <button class="btn btn-ghost" @click="showAddModal = false">Cancel</button>
          <button class="btn btn-primary" @click="addGate" :disabled="saving">
            {{ saving ? 'Adding...' : 'Add Gate' }}
          </button>
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

interface Gate {
  id: string
  name: string
  condition: string
  action: 'block' | 'warn' | 'log'
  active: boolean
  triggeredCount: number
  description: string
}
interface Event {
  id: string
  time: string
  gate: string
  action: string
  context: string
  tenant: string
}

const loading = ref(false)
const saving = ref(false)
const showAddModal = ref(false)
const gates = ref<Gate[]>([])
const events = ref<Event[]>([])
const newGate = ref({ name: '', condition: '', action: 'warn', description: '' })

const demoGates: Gate[] = [
  { id: 'g1', name: 'Token Budget Guard', condition: 'token_usage > 0.9 * daily_limit', action: 'warn', active: true, triggeredCount: 24, description: 'Warn when tenant approaches daily token limit' },
  { id: 'g2', name: 'LLM Overspend Block', condition: 'cost_per_hour > 5.00', action: 'block', active: true, triggeredCount: 3, description: 'Block requests if hourly LLM cost exceeds $5' },
  { id: 'g3', name: 'High Entropy Alert', condition: 'output_entropy > 0.95', action: 'log', active: true, triggeredCount: 18, description: 'Log when LLM output entropy is extremely high' },
  { id: 'g4', name: 'Cross-Tenant Isolation', condition: 'tenant_id != context.tenant_id', action: 'block', active: true, triggeredCount: 0, description: 'Block any cross-tenant data access attempts' },
  { id: 'g5', name: 'Agent Loop Detector', condition: 'agent_iterations > 20', action: 'block', active: false, triggeredCount: 7, description: 'Block agents stuck in infinite loops (disabled for debug)' },
]

const demoEvents: Event[] = [
  { id: 'e1', time: '14:31:02', gate: 'Token Budget Guard', action: 'warn', context: 'Tenant tenant-002 at 96% daily budget', tenant: 'tenant-002' },
  { id: 'e2', time: '14:28:44', gate: 'High Entropy Alert', action: 'log', context: 'PlannerAgent output entropy=0.991 model=gpt-4o temp=0.9', tenant: 'tenant-001' },
  { id: 'e3', time: '14:22:17', gate: 'LLM Overspend Block', action: 'block', context: 'Hourly cost=$5.42 exceeded threshold', tenant: 'tenant-004' },
  { id: 'e4', time: '14:18:33', gate: 'Token Budget Guard', action: 'warn', context: 'Tenant tenant-004 at 92% daily budget', tenant: 'tenant-004' },
  { id: 'e5', time: '13:55:01', gate: 'High Entropy Alert', action: 'log', context: 'RAGAgent output entropy=0.973 model=claude-sonnet-4-6', tenant: 'tenant-001' },
]

const activeCount = computed(() => gates.value.filter(g => g.active).length)
const inactiveCount = computed(() => gates.value.filter(g => !g.active).length)
const totalTriggeredToday = computed(() => gates.value.reduce((s, g) => s + g.triggeredCount, 0))
const totalBlocked = computed(() => events.value.filter(e => e.action === 'block').length)

function actionBadge(action: string) {
  if (action === 'block') return 'badge-err'
  if (action === 'warn') return 'badge-warn'
  return 'badge-info'
}

async function toggleGate(gate: Gate) {
  const prev = gate.active
  gate.active = !gate.active
  try {
    await api('/gates/toggle', { method: 'POST', body: JSON.stringify({ id: gate.id, active: gate.active }) })
    store.showToast(`${gate.name} ${gate.active ? 'activated' : 'deactivated'}`, 'ok')
  } catch {
    gate.active = prev
    store.showToast('Failed to toggle gate', 'err')
  }
}

async function addGate() {
  saving.value = true
  try {
    const res = await api<any>('/gates', {
      method: 'POST',
      body: JSON.stringify(newGate.value)
    })
    gates.value.unshift(res.gate ?? {
      id: 'g' + Date.now(),
      ...newGate.value,
      active: true,
      triggeredCount: 0
    })
    store.showToast('Gate added', 'ok')
    showAddModal.value = false
    newGate.value = { name: '', condition: '', action: 'warn', description: '' }
  } catch {
    store.showToast('Failed to add gate', 'err')
  } finally {
    saving.value = false
  }
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/gates')
    gates.value = res.gates ?? res
    events.value = res.events ?? demoEvents
  } catch {
    gates.value = demoGates
    events.value = demoEvents
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.gates-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.kpi-card { flex: 1; min-width: 130px; padding: .75rem 1rem; }
.kpi-label { font-size: .7rem; letter-spacing: .06em; margin-bottom: .25rem; }
.kpi-value { font-size: 1.75rem; font-weight: 700; font-family: var(--font-mono); }

.section-title { font-weight: 700; font-size: .9rem; padding: .75rem 1rem; border-bottom: 1px solid var(--panel-border); }

.inactive-row { opacity: .55; }

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
.toggle-switch input:checked + .toggle-slider { background: var(--accent3); }
.toggle-switch input:checked + .toggle-slider::before { transform: translateX(16px); background: #fff; }

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
