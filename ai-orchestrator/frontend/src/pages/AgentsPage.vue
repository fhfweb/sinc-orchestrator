<template>
  <div class="agents-page">
    <div class="flex justify-between items-center">
      <h1 class="page-title">Agentes</h1>
      <div class="flex gap-2">
        <select v-model="statusFilter">
          <option value="">Todos</option>
          <option>idle</option><option>thinking</option><option>executing</option><option>error</option>
        </select>
        <button class="btn btn-ghost" @click="load">↻</button>
      </div>
    </div>

    <div class="agents-grid">
      <div v-for="a in filtered" :key="a.id" class="agent-card card" :class="`status-${a.status}`">
        <div class="ac-head flex justify-between items-center">
          <div class="flex items-center gap-2">
            <span class="ac-dot"></span>
            <span class="ac-name">{{ a.name }}</span>
          </div>
          <span class="badge" :class="statusBadge(a.status)">{{ a.status }}</span>
        </div>

        <div class="ac-meta mt-2">
          <div class="ac-row">
            <span class="text-muted">ID</span>
            <span class="mono" style="font-size:10px">{{ a.id.substring(0,16) }}…</span>
          </div>
          <div class="ac-row" v-if="a.current_task">
            <span class="text-muted">Task</span>
            <span class="truncate">{{ a.current_task }}</span>
          </div>
          <div class="ac-row" v-if="a.model">
            <span class="text-muted">Model</span>
            <span>{{ a.model }}</span>
          </div>
          <div class="ac-row">
            <span class="text-muted">Tasks done</span>
            <span class="text-ok">{{ a.tasks_completed ?? 0 }}</span>
          </div>
        </div>

        <div class="ac-actions flex gap-2 mt-2">
          <button class="btn btn-ghost" style="font-size:11px" @click="pauseAgent(a.id)">⏸</button>
          <button class="btn btn-ghost" style="font-size:11px" @click="killAgent(a.id)">⏹</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api, apif } = useApi()
const app = useAppStore()
const statusFilter = ref('')

interface Agent {
  id: string
  name: string
  status: string
  current_task?: string
  model?: string
  tasks_completed?: number
}

const agents = ref<Agent[]>([])

const filtered = computed(() =>
  agents.value.filter(a => !statusFilter.value || a.status === statusFilter.value)
)

function statusBadge(s: string) {
  return { 'badge-ok': s === 'idle', 'badge-info': s === 'thinking', 'badge-warn': s === 'executing', 'badge-err': s === 'error' }
}

async function load() {
  try {
    const d = await api<{ agents: Agent[] }>('/agents/roster')
    agents.value = d.agents ?? []
  } catch {
    agents.value = [
      { id: 'orch-0001', name: 'Orchestrator', status: 'executing', current_task: 'task-dispatch', tasks_completed: 1842 },
      { id: 'rag-0001', name: 'RAG-Engine', status: 'idle', tasks_completed: 934 },
      { id: 'llm-0001', name: 'LLM-Router', status: 'thinking', current_task: 'completion', model: 'gpt-4o-mini', tasks_completed: 2201 },
      { id: 'mem-0001', name: 'Memory-Sync', status: 'idle', tasks_completed: 512 },
      { id: 'cog-0001', name: 'Cognitive-Core', status: 'executing', current_task: 'mcts-search', tasks_completed: 389 },
      { id: 'sup-0001', name: 'Supervisor', status: 'idle', tasks_completed: 721 }
    ]
  }
}

async function pauseAgent(id: string) {
  try { await apif('/agents/pause', { agent_id: id }); app.showToast('Agente pausado', 'ok') }
  catch { app.showToast('Falha ao pausar agente', 'err') }
}

async function killAgent(id: string) {
  try { await apif('/agents/kill', { agent_id: id }); app.showToast('Agente encerrado', 'warn'); load() }
  catch { app.showToast('Falha ao encerrar agente', 'err') }
}

onMounted(load)
</script>

<style scoped>
.agents-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }

.agents-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.agent-card { transition: var(--transition); border-left: 3px solid var(--panel-border); }
.status-idle { border-left-color: var(--text-muted); }
.status-thinking { border-left-color: var(--accent); }
.status-executing { border-left-color: var(--accent3); }
.status-error { border-left-color: var(--danger); }

.ac-dot {
  width: 8px; height: 8px; border-radius: 50%;
  background: var(--text-muted);
  flex-shrink: 0;
}
.status-thinking .ac-dot { background: var(--accent); box-shadow: 0 0 6px var(--accent); }
.status-executing .ac-dot { background: var(--accent3); box-shadow: 0 0 6px var(--accent3); }
.status-error .ac-dot { background: var(--danger); }

.ac-name { font-weight: 600; font-size: 13px; }

.ac-meta { display: flex; flex-direction: column; gap: 4px; }
.ac-row { display: flex; justify-content: space-between; font-size: 11.5px; gap: 8px; }
.ac-row > span:first-child { flex-shrink: 0; }

.ac-actions button { padding: 4px 10px; }
</style>
