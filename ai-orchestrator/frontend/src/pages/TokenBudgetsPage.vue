<template>
  <div class="token-budgets-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Token Budget Management</h1>
        <p class="text-muted">Configure and monitor token consumption limits per tenant</p>
      </div>
      <button class="btn btn-ghost" @click="loadData" :disabled="loading">
        {{ loading ? 'Refreshing...' : 'Refresh' }}
      </button>
    </div>

    <div class="card" style="overflow-x: auto;">
      <table class="tbl">
        <thead>
          <tr>
            <th>Tenant</th>
            <th>Daily Limit</th>
            <th>Used Today</th>
            <th>Monthly Limit</th>
            <th>Used Month</th>
            <th>Alert Threshold</th>
            <th>Actions</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="tenant in budgets" :key="tenant.id">
            <td>
              <span class="font-semibold">{{ tenant.name }}</span>
              <div class="text-muted mono" style="font-size:.7rem;">{{ tenant.id }}</div>
            </td>
            <td class="mono">{{ tenant.dailyLimit.toLocaleString() }}</td>
            <td>
              <div class="mono" :class="usageClass(tenant.usedToday, tenant.dailyLimit)">
                {{ tenant.usedToday.toLocaleString() }}
              </div>
              <div class="progress-bar-wrap">
                <div
                  class="progress-bar-fill"
                  :class="progressClass(tenant.usedToday, tenant.dailyLimit)"
                  :style="{ width: progressPct(tenant.usedToday, tenant.dailyLimit) + '%' }"
                ></div>
              </div>
              <div class="text-muted" style="font-size:.7rem;">{{ progressPct(tenant.usedToday, tenant.dailyLimit) }}%</div>
            </td>
            <td class="mono">{{ tenant.monthlyLimit.toLocaleString() }}</td>
            <td>
              <div class="mono" :class="usageClass(tenant.usedMonth, tenant.monthlyLimit)">
                {{ tenant.usedMonth.toLocaleString() }}
              </div>
              <div class="progress-bar-wrap">
                <div
                  class="progress-bar-fill"
                  :class="progressClass(tenant.usedMonth, tenant.monthlyLimit)"
                  :style="{ width: progressPct(tenant.usedMonth, tenant.monthlyLimit) + '%' }"
                ></div>
              </div>
              <div class="text-muted" style="font-size:.7rem;">{{ progressPct(tenant.usedMonth, tenant.monthlyLimit) }}%</div>
            </td>
            <td>
              <span class="badge" :class="thresholdBadge(tenant.alertThreshold)">{{ tenant.alertThreshold }}%</span>
            </td>
            <td>
              <button class="btn btn-ghost" style="font-size:.75rem; padding:.3rem .6rem;" @click="openEdit(tenant)">
                Edit
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Edit Modal -->
    <div v-if="editModal" class="modal-backdrop" @click.self="editModal = false">
      <div class="card modal-box">
        <h2 class="modal-title">Edit Budget — {{ editTarget?.name }}</h2>
        <div class="form-grid">
          <label class="form-label">
            Daily Limit (tokens)
            <input class="form-input mono" type="number" v-model.number="editForm.dailyLimit" />
          </label>
          <label class="form-label">
            Monthly Limit (tokens)
            <input class="form-input mono" type="number" v-model.number="editForm.monthlyLimit" />
          </label>
          <label class="form-label">
            Alert Threshold (%)
            <input class="form-input mono" type="number" min="0" max="100" v-model.number="editForm.alertThreshold" />
          </label>
        </div>
        <div class="flex gap-2" style="margin-top:1rem; justify-content: flex-end;">
          <button class="btn btn-ghost" @click="editModal = false">Cancel</button>
          <button class="btn btn-primary" @click="saveBudget" :disabled="saving">
            {{ saving ? 'Saving...' : 'Save' }}
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

interface TenantBudget {
  id: string
  name: string
  dailyLimit: number
  usedToday: number
  monthlyLimit: number
  usedMonth: number
  alertThreshold: number
}

const loading = ref(false)
const saving = ref(false)
const budgets = ref<TenantBudget[]>([])
const editModal = ref(false)
const editTarget = ref<TenantBudget | null>(null)
const editForm = ref({ dailyLimit: 0, monthlyLimit: 0, alertThreshold: 80 })

const demoBudgets: TenantBudget[] = [
  { id: 'tenant-001', name: 'Acme Corp', dailyLimit: 1000000, usedToday: 342000, monthlyLimit: 25000000, usedMonth: 8200000, alertThreshold: 80 },
  { id: 'tenant-002', name: 'Globex Inc', dailyLimit: 500000, usedToday: 478000, monthlyLimit: 12000000, usedMonth: 11500000, alertThreshold: 90 },
  { id: 'tenant-003', name: 'Initech', dailyLimit: 200000, usedToday: 45000, monthlyLimit: 5000000, usedMonth: 1200000, alertThreshold: 75 },
  { id: 'tenant-004', name: 'Umbrella Ltd', dailyLimit: 2000000, usedToday: 1850000, monthlyLimit: 50000000, usedMonth: 32000000, alertThreshold: 85 },
  { id: 'tenant-005', name: 'Wayne Ent.', dailyLimit: 750000, usedToday: 120000, monthlyLimit: 18000000, usedMonth: 4300000, alertThreshold: 70 },
]

function progressPct(used: number, limit: number) {
  return Math.min(100, Math.round((used / limit) * 100))
}
function progressClass(used: number, limit: number) {
  const p = progressPct(used, limit)
  if (p < 60) return 'fill-ok'
  if (p < 85) return 'fill-warn'
  return 'fill-err'
}
function usageClass(used: number, limit: number) {
  const p = progressPct(used, limit)
  if (p < 60) return 'text-ok'
  if (p < 85) return 'text-warn'
  return 'text-danger'
}
function thresholdBadge(t: number) {
  if (t <= 75) return 'badge-ok'
  if (t <= 85) return 'badge-warn'
  return 'badge-err'
}

function openEdit(tenant: TenantBudget) {
  editTarget.value = tenant
  editForm.value = { dailyLimit: tenant.dailyLimit, monthlyLimit: tenant.monthlyLimit, alertThreshold: tenant.alertThreshold }
  editModal.value = true
}

async function saveBudget() {
  if (!editTarget.value) return
  saving.value = true
  try {
    await api('/token-budgets', {
      method: 'POST',
      body: JSON.stringify({ tenantId: editTarget.value.id, ...editForm.value })
    })
    const idx = budgets.value.findIndex(b => b.id === editTarget.value!.id)
    if (idx >= 0) budgets.value[idx] = { ...budgets.value[idx], ...editForm.value }
    store.showToast('Budget saved', 'ok')
    editModal.value = false
  } catch {
    store.showToast('Failed to save budget', 'err')
  } finally {
    saving.value = false
  }
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/token-budgets')
    budgets.value = res.budgets ?? res
  } catch {
    budgets.value = demoBudgets
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.token-budgets-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }
.font-semibold { font-weight: 600; }

.progress-bar-wrap {
  height: 4px;
  background: var(--bg3);
  border-radius: 2px;
  margin: .25rem 0;
  width: 120px;
}
.progress-bar-fill {
  height: 100%;
  border-radius: 2px;
  transition: width .4s ease;
}
.fill-ok { background: var(--accent3); }
.fill-warn { background: var(--warn); }
.fill-err { background: var(--danger); }

.modal-backdrop {
  position: fixed; inset: 0;
  background: rgba(0,0,0,.6);
  display: flex; align-items: center; justify-content: center;
  z-index: 200;
}
.modal-box { padding: 1.5rem; min-width: 380px; max-width: 500px; width: 100%; }
.modal-title { font-size: 1.1rem; font-weight: 700; margin: 0 0 1rem; }
.form-grid { display: flex; flex-direction: column; gap: .75rem; }
.form-label { display: flex; flex-direction: column; gap: .35rem; font-size: .85rem; color: var(--text-muted); }
.form-input {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .45rem .65rem;
  font-size: .9rem;
  outline: none;
  transition: var(--transition);
}
.form-input:focus { border-color: var(--accent); }
</style>
