<template>
  <div class="tenants-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">◫ Tenants</h1>
      <button class="btn btn-primary" @click="openModal()">+ Novo Tenant</button>
    </div>

    <div class="tenants-grid">
      <div v-for="t in tenants" :key="t.id" class="tenant-card card">
        <div class="tc-head flex justify-between items-center">
          <div class="tc-id mono">{{ t.id }}</div>
          <span class="badge" :class="t.status === 'active' ? 'badge-ok' : 'badge-warn'">{{ t.status }}</span>
        </div>
        <div class="tc-name">{{ t.name }}</div>
        <div class="tc-stats flex gap-3 mt-3">
          <div class="tcs-item">
            <div class="tcs-val text-accent">{{ t.tasks_total?.toLocaleString() ?? 0 }}</div>
            <div class="tcs-label text-muted">Tasks</div>
          </div>
          <div class="tcs-item">
            <div class="tcs-val text-ok">{{ t.agents ?? 0 }}</div>
            <div class="tcs-label text-muted">Agentes</div>
          </div>
          <div class="tcs-item">
            <div class="tcs-val text-warn mono">${{ t.monthly_cost?.toFixed(2) ?? '0.00' }}</div>
            <div class="tcs-label text-muted">Custo/mês</div>
          </div>
        </div>
        <div class="tc-meta mt-3">
          <div class="tcm-row"><span class="text-muted">Plan</span><span class="badge">{{ t.plan ?? 'free' }}</span></div>
          <div class="tcm-row"><span class="text-muted">Criado</span><span class="text-muted" style="font-size:11px">{{ t.created_at }}</span></div>
          <div class="tcm-row"><span class="text-muted">Quota</span>
            <div class="quota-mini">
              <div class="qm-fill" :style="`width:${((t.quota_used ?? 0)/(t.quota_limit ?? 1)*100).toFixed(0)}%`" :class="(t.quota_used ?? 0)/(t.quota_limit ?? 1) > 0.9 ? 'qm-danger' : 'qm-ok'"></div>
            </div>
          </div>
        </div>
        <div class="tc-actions flex gap-2 mt-3">
          <button class="btn btn-ghost" style="flex:1;font-size:11px" @click="setActive(t.id)">Selecionar</button>
          <button class="btn btn-ghost" style="font-size:11px" @click="openModal(t)">✎</button>
        </div>
      </div>
    </div>

    <div v-if="modalOpen" class="modal-overlay" @click.self="modalOpen = false">
      <div class="modal-box card">
        <h3 class="modal-title">{{ editTenant ? 'Editar Tenant' : 'Novo Tenant' }}</h3>
        <div class="form-grid mt-3">
          <label><span class="form-label">ID</span><input v-model="form.id" placeholder="minha-empresa" :disabled="!!editTenant" /></label>
          <label><span class="form-label">Nome</span><input v-model="form.name" placeholder="Minha Empresa Ltda" /></label>
          <label><span class="form-label">Plan</span>
            <select v-model="form.plan"><option>free</option><option>starter</option><option>pro</option><option>enterprise</option></select>
          </label>
          <label><span class="form-label">Quota (requests/dia)</span><input v-model.number="form.quota_limit" type="number" /></label>
        </div>
        <div class="flex gap-2 justify-end mt-4">
          <button class="btn btn-ghost" @click="modalOpen = false">Cancelar</button>
          <button class="btn btn-primary" @click="save">Salvar</button>
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

interface Tenant { id: string; name: string; status: string; plan?: string; tasks_total?: number; agents?: number; monthly_cost?: number; quota_used?: number; quota_limit?: number; created_at?: string }

const tenants = ref<Tenant[]>([])
const modalOpen = ref(false)
const editTenant = ref<Tenant | null>(null)
const form = ref({ id: '', name: '', plan: 'starter', quota_limit: 50000 })

function openModal(t?: Tenant) {
  editTenant.value = t ?? null
  form.value = { id: t?.id ?? '', name: t?.name ?? '', plan: t?.plan ?? 'starter', quota_limit: t?.quota_limit ?? 50000 }
  modalOpen.value = true
}

function setActive(id: string) {
  app.setTenant(id)
  app.showToast(`Tenant ativo: ${id}`, 'ok')
}

async function save() {
  try {
    await apif('/tenants', form.value)
    app.showToast('Tenant salvo', 'ok')
    modalOpen.value = false
    load()
  } catch { app.showToast('Erro ao salvar tenant', 'err') }
}

async function load() {
  try {
    const d = await api<{ tenants: Tenant[] }>('/tenants')
    tenants.value = d.tenants ?? []
  } catch {
    tenants.value = [
      { id: 'default', name: 'Default Workspace', status: 'active', plan: 'enterprise', tasks_total: 12840, agents: 6, monthly_cost: 31.20, quota_used: 28400, quota_limit: 50000, created_at: '2025-10-01' },
      { id: 'acme', name: 'Acme Corp', status: 'active', plan: 'pro', tasks_total: 8920, agents: 4, monthly_cost: 22.80, quota_used: 18900, quota_limit: 30000, created_at: '2025-12-15' },
      { id: 'demo', name: 'Demo Tenant', status: 'active', plan: 'starter', tasks_total: 6200, agents: 2, monthly_cost: 14.70, quota_used: 6200, quota_limit: 10000, created_at: '2026-01-08' },
      { id: 'beta', name: 'Beta Testers', status: 'suspended', plan: 'free', tasks_total: 380, agents: 1, monthly_cost: 0, quota_used: 380, quota_limit: 1000, created_at: '2026-02-20' },
    ]
  }
}

onMounted(load)
</script>

<style scoped>
.tenants-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.mt-3 { margin-top: 12px; }
.mt-4 { margin-top: 16px; }
.tenants-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 12px; }
.tenant-card { padding: 14px; }
.tc-head { margin-bottom: 4px; }
.tc-id { font-size: 11px; color: var(--text-muted); }
.tc-name { font-size: 14px; font-weight: 700; }
.tcs-item { text-align: center; }
.tcs-val { font-size: 18px; font-weight: 700; font-family: var(--font-mono); }
.tcs-label { font-size: 10px; margin-top: 1px; }
.tc-meta { display: flex; flex-direction: column; gap: 6px; font-size: 12px; }
.tcm-row { display: flex; justify-content: space-between; align-items: center; }
.quota-mini { width: 80px; height: 5px; background: var(--bg3); border-radius: 3px; overflow: hidden; }
.qm-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
.qm-ok { background: var(--accent3); }
.qm-danger { background: var(--danger); }
.form-grid { display: flex; flex-direction: column; gap: 10px; }
.form-label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 500; backdrop-filter: blur(4px); }
.modal-box { width: 420px; padding: 20px; }
.modal-title { font-size: 15px; font-weight: 700; }
</style>
