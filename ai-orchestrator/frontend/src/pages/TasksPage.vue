<template>
  <div class="tasks-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">◈ Tarefas</h1>
      <div class="flex gap-2">
        <select v-model="statusFilter" @change="load"><option value="">Todos</option><option>pending</option><option>running</option><option>completed</option><option>failed</option></select>
        <input v-model="search" placeholder="Buscar..." style="width:180px" />
        <button class="btn btn-ghost" @click="load">↻</button>
      </div>
    </div>

    <!-- Stats bar -->
    <div class="task-stats">
      <div class="ts-card card" v-for="s in stats" :key="s.label">
        <div class="ts-val" :class="s.cls">{{ s.val }}</div>
        <div class="ts-label text-muted">{{ s.label }}</div>
      </div>
    </div>

    <!-- Batch actions -->
    <div class="card batch-bar" v-if="selected.size > 0">
      <span>{{ selected.size }} selecionadas</span>
      <button class="btn btn-ghost" @click="batchAction('cancel')">Cancelar</button>
      <button class="btn btn-ghost" @click="batchAction('retry')">Retry</button>
      <button class="btn btn-danger" @click="batchAction('delete')">Deletar</button>
      <button class="btn btn-ghost" @click="selected.clear()">Limpar seleção</button>
    </div>

    <div class="card">
      <table class="tbl">
        <thead>
          <tr>
            <th><input type="checkbox" @change="toggleAll" /></th>
            <th>ID</th><th>Tipo</th><th>Status</th><th>Prioridade</th><th>Agente</th><th>Criada em</th><th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in filtered" :key="t.id">
            <td><input type="checkbox" :checked="selected.has(t.id)" @change="toggleSelect(t.id)" /></td>
            <td class="mono" style="font-size:10px">{{ t.id.substring(0,12) }}…</td>
            <td>{{ t.type }}</td>
            <td><span class="badge" :class="statusBadge(t.status)">{{ t.status }}</span></td>
            <td><span class="badge" :class="prioBadge(t.priority)">{{ t.priority ?? 'normal' }}</span></td>
            <td class="text-muted mono" style="font-size:11px">{{ t.agent_id ? t.agent_id.substring(0,10) : '—' }}</td>
            <td class="text-muted" style="font-size:11px">{{ t.created_at }}</td>
            <td>
              <button class="btn btn-ghost" style="font-size:11px;padding:2px 6px" @click="cancelTask(t.id)" :disabled="t.status === 'completed'">✕</button>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="filtered.length === 0" class="text-muted" style="padding:20px;text-align:center">Nenhuma tarefa encontrada</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api, apif } = useApi()
const app = useAppStore()

interface Task { id: string; type: string; status: string; priority?: string; agent_id?: string; created_at?: string }

const tasks = ref<Task[]>([])
const statusFilter = ref('')
const search = ref('')
const selected = ref(new Set<string>())

const stats = ref([
  { label: 'Total', val: '0', cls: 'text-accent' },
  { label: 'Running', val: '0', cls: 'text-ok' },
  { label: 'Pending', val: '0', cls: 'text-warn' },
  { label: 'Failed', val: '0', cls: 'text-danger' },
  { label: 'Completed', val: '0', cls: 'text-muted' },
])

const filtered = computed(() => tasks.value.filter(t => {
  if (statusFilter.value && t.status !== statusFilter.value) return false
  if (search.value && !JSON.stringify(t).toLowerCase().includes(search.value.toLowerCase())) return false
  return true
}))

function statusBadge(s: string) {
  return { 'badge-ok': s === 'completed', 'badge-warn': s === 'running', 'badge-err': s === 'failed', 'badge-info': s === 'pending' }
}
function prioBadge(p?: string) {
  return { 'badge-err': p === 'critical', 'badge-warn': p === 'high' }
}

function toggleAll(e: Event) {
  if ((e.target as HTMLInputElement).checked) filtered.value.forEach(t => selected.value.add(t.id))
  else selected.value.clear()
}
function toggleSelect(id: string) {
  if (selected.value.has(id)) selected.value.delete(id)
  else selected.value.add(id)
}

async function cancelTask(id: string) {
  try {
    await apif('/tasks/cancel', { task_id: id })
    app.showToast('Tarefa cancelada', 'ok')
    load()
  } catch { app.showToast('Erro ao cancelar', 'err') }
}

async function batchAction(action: string) {
  try {
    await apif('/tasks/batch', { action, task_ids: [...selected.value] })
    app.showToast(`Batch ${action} executado`, 'ok')
    selected.value.clear()
    load()
  } catch { app.showToast('Erro no batch', 'err') }
}

function updateStats() {
  const t = tasks.value
  stats.value[0].val = String(t.length)
  stats.value[1].val = String(t.filter(x => x.status === 'running').length)
  stats.value[2].val = String(t.filter(x => x.status === 'pending').length)
  stats.value[3].val = String(t.filter(x => x.status === 'failed').length)
  stats.value[4].val = String(t.filter(x => x.status === 'completed').length)
}

async function load() {
  try {
    const d = await api<{ tasks: Task[] }>('/tasks/list', { params: { status: statusFilter.value || undefined } })
    tasks.value = d.tasks ?? []
  } catch {
    tasks.value = Array.from({ length: 12 }, (_, i) => ({
      id: `task-${i.toString().padStart(8,'0')}-0000-0000-0000-000000000000`,
      type: ['rag_query','llm_completion','memory_sync','web_search'][i % 4],
      status: ['pending','running','completed','failed'][i % 4],
      priority: ['low','normal','high','critical'][i % 4],
      created_at: new Date(Date.now() - i * 120000).toISOString().substring(0, 19).replace('T', ' ')
    }))
  }
  updateStats()
}

onMounted(load)
</script>

<style scoped>
.tasks-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.task-stats { display: grid; grid-template-columns: repeat(5,1fr); gap: 10px; }
.ts-card { text-align: center; padding: 12px; }
.ts-val { font-size: 24px; font-weight: 800; font-family: var(--font-mono); }
.ts-label { font-size: 11px; margin-top: 2px; }
.batch-bar { display: flex; align-items: center; gap: 10px; padding: 10px 14px; background: rgba(0,212,255,0.05); border-color: rgba(0,212,255,0.2); }
</style>
