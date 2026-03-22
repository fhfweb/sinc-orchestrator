<template>
  <div class="kanban-page">
    <div class="kb-header flex justify-between items-center">
      <h1 class="page-title">Job Board</h1>
      <div class="flex gap-2">
        <input v-model="search" placeholder="Buscar tarefa..." style="width:200px" />
        <button class="btn btn-ghost" @click="load">↻ Refresh</button>
      </div>
    </div>

    <div class="kb-board">
      <div v-for="col in columns" :key="col.id" class="kb-col">
        <div class="kb-col-head">
          <span>{{ col.label }}</span>
          <span class="badge">{{ tasksByStatus(col.id).length }}</span>
        </div>
        <div class="kb-cards">
          <div
            v-for="t in tasksByStatus(col.id)"
            :key="t.id"
            class="kb-card card"
            :class="`prio-${t.priority}`"
          >
            <div class="kbc-id mono text-muted">{{ t.id.substring(0, 8) }}</div>
            <div class="kbc-title">{{ t.title ?? t.type ?? 'Task' }}</div>
            <div class="kbc-meta flex justify-between items-center mt-2">
              <span class="badge" :class="prioBadge(t.priority)">{{ t.priority ?? 'normal' }}</span>
              <span class="text-muted" style="font-size:10px">{{ t.agent_id ? '◈ ' + t.agent_id.substring(0,8) : '' }}</span>
            </div>
          </div>
          <div v-if="tasksByStatus(col.id).length === 0" class="kb-empty text-muted">vazio</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'

const { api } = useApi()
const search = ref('')

interface Task {
  id: string
  status: string
  title?: string
  type?: string
  priority?: string
  agent_id?: string
}

const tasks = ref<Task[]>([])

const columns = [
  { id: 'pending', label: 'Pendente' },
  { id: 'running', label: 'Executando' },
  { id: 'completed', label: 'Concluído' },
  { id: 'failed', label: 'Falhou' }
]

function tasksByStatus(status: string) {
  return tasks.value.filter(t =>
    t.status === status &&
    (!search.value || JSON.stringify(t).toLowerCase().includes(search.value.toLowerCase()))
  )
}

function prioBadge(p?: string) {
  if (p === 'high') return 'badge-warn'
  if (p === 'critical') return 'badge-err'
  return ''
}

async function load() {
  try {
    const data = await api<{ tasks: Task[] }>('/tasks/list')
    tasks.value = data.tasks ?? []
  } catch {
    // demo data
    tasks.value = [
      { id: 'aabb1122-0000-0000-0000-000000000001', status: 'pending', type: 'rag_query', priority: 'normal' },
      { id: 'aabb1122-0000-0000-0000-000000000002', status: 'running', type: 'llm_completion', priority: 'high', agent_id: 'agent-00000001' },
      { id: 'aabb1122-0000-0000-0000-000000000003', status: 'completed', type: 'memory_sync', priority: 'low' },
      { id: 'aabb1122-0000-0000-0000-000000000004', status: 'failed', type: 'web_search', priority: 'normal' }
    ]
  }
}

onMounted(load)
</script>

<style scoped>
.kanban-page { display: flex; flex-direction: column; gap: 16px; height: 100%; }
.page-title { font-size: 16px; font-weight: 700; }

.kb-board {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 12px;
  flex: 1;
  overflow: hidden;
}

.kb-col {
  display: flex;
  flex-direction: column;
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius);
  overflow: hidden;
}

.kb-col-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 12px;
  font-size: 12px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  border-bottom: 1px solid var(--panel-border);
  background: var(--bg3);
}

.kb-cards {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.kb-card {
  padding: 10px;
  cursor: pointer;
  transition: var(--transition);
  border-left: 3px solid transparent;
}
.kb-card:hover { transform: translateY(-1px); }
.prio-high { border-left-color: var(--warn); }
.prio-critical { border-left-color: var(--danger); }

.kbc-id { font-size: 10px; margin-bottom: 4px; }
.kbc-title { font-size: 12.5px; font-weight: 500; }

.kb-empty { text-align: center; padding: 16px; font-size: 12px; }

@media (max-width: 900px) {
  .kb-board { grid-template-columns: 1fr 1fr; }
}
</style>
