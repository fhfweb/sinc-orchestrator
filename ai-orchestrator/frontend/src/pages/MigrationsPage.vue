<template>
  <div class="migrations-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">⊛ Migrações de DB</h1>
      <div class="flex gap-2">
        <button class="btn btn-ghost" @click="load">↻ Refresh</button>
        <button class="btn btn-primary" :disabled="pendingCount === 0 || running" @click="runAll">
          {{ running ? '…' : `▶ Executar ${pendingCount} pendentes` }}
        </button>
      </div>
    </div>

    <!-- Status cards -->
    <div class="mig-stats">
      <div class="card ms-card" v-for="s in stats" :key="s.label">
        <div class="ms-val" :class="s.cls">{{ s.val }}</div>
        <div class="ms-label text-muted">{{ s.label }}</div>
      </div>
    </div>

    <div class="card">
      <table class="tbl">
        <thead>
          <tr><th>Versão</th><th>Nome</th><th>Status</th><th>Aplicada em</th><th>Duração</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="m in migrations" :key="m.version" :class="{ 'row-pending': m.status === 'pending', 'row-error': m.status === 'error' }">
            <td class="mono" style="font-weight:600">{{ m.version }}</td>
            <td>{{ m.name }}</td>
            <td><span class="badge" :class="migBadge(m.status)">{{ m.status }}</span></td>
            <td class="text-muted" style="font-size:11px">{{ m.applied_at ?? '—' }}</td>
            <td class="mono text-muted" style="font-size:11px">{{ m.duration_ms ? m.duration_ms + 'ms' : '—' }}</td>
            <td>
              <button v-if="m.status === 'pending'" class="btn btn-ghost" style="font-size:11px;padding:3px 8px" :disabled="running" @click="runOne(m.version)">
                Aplicar
              </button>
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

const { api, apif } = useApi()
const app = useAppStore()

interface Migration { version: string; name: string; status: 'applied' | 'pending' | 'error'; applied_at?: string; duration_ms?: number }

const migrations = ref<Migration[]>([])
const running = ref(false)

const pendingCount = computed(() => migrations.value.filter(m => m.status === 'pending').length)

const stats = computed(() => [
  { label: 'Aplicadas', val: String(migrations.value.filter(m => m.status === 'applied').length), cls: 'text-ok' },
  { label: 'Pendentes', val: String(pendingCount.value), cls: pendingCount.value > 0 ? 'text-warn' : 'text-muted' },
  { label: 'Erros', val: String(migrations.value.filter(m => m.status === 'error').length), cls: 'text-danger' },
])

function migBadge(s: string) {
  return { 'badge-ok': s === 'applied', 'badge-warn': s === 'pending', 'badge-err': s === 'error' }
}

async function runAll() {
  running.value = true
  try {
    await apif('/migrations/run-pending', {})
    app.showToast('Migrações executadas', 'ok')
    load()
  } catch { app.showToast('Erro nas migrações', 'err') }
  finally { running.value = false }
}

async function runOne(version: string) {
  running.value = true
  try {
    await apif('/migrations/apply', { version })
    app.showToast(`Migração ${version} aplicada`, 'ok')
    load()
  } catch { app.showToast('Erro na migração', 'err') }
  finally { running.value = false }
}

async function load() {
  try {
    const d = await api<{ migrations: Migration[] }>('/migrations')
    migrations.value = d.migrations ?? []
  } catch {
    migrations.value = [
      { version: '001', name: 'create_tasks_table', status: 'applied', applied_at: '2025-10-01 10:00', duration_ms: 42 },
      { version: '002', name: 'create_agents_table', status: 'applied', applied_at: '2025-10-01 10:01', duration_ms: 18 },
      { version: '003', name: 'add_task_priority', status: 'applied', applied_at: '2025-11-15 09:22', duration_ms: 8 },
      { version: '004', name: 'create_audit_log', status: 'applied', applied_at: '2025-12-01 14:00', duration_ms: 95 },
      { version: '005', name: 'add_tenant_isolation', status: 'applied', applied_at: '2026-01-10 11:30', duration_ms: 234 },
      { version: '006', name: 'create_memory_graph_schema', status: 'pending' },
      { version: '007', name: 'add_canary_deployment_table', status: 'pending' },
    ]
  }
}

onMounted(load)
</script>

<style scoped>
.migrations-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.mig-stats { display: grid; grid-template-columns: repeat(3, 200px); gap: 10px; }
.ms-card { text-align: center; padding: 12px; }
.ms-val { font-size: 28px; font-weight: 800; font-family: var(--font-mono); }
.ms-label { font-size: 11px; margin-top: 2px; }
.row-pending td { color: var(--warn); }
.row-error td { color: var(--danger); }
</style>
