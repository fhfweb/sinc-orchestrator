<template>
  <div class="context-traces-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Context Window Traces</h1>
        <p class="text-muted">Monitor agent context usage and message history</p>
      </div>
      <button class="btn btn-ghost" @click="loadData" :disabled="loading">
        {{ loading ? 'Refreshing...' : 'Refresh' }}
      </button>
    </div>

    <div class="layout" :class="{ 'with-detail': selectedTrace }">
      <div class="card table-panel" style="overflow-x: auto;">
        <table class="tbl">
          <thead>
            <tr>
              <th>Session ID</th>
              <th>Agent</th>
              <th>Tokens Used</th>
              <th>Max Tokens</th>
              <th>% Full</th>
              <th>Messages</th>
              <th>Last Activity</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="trace in traces"
              :key="trace.sessionId"
              class="clickable-row"
              :class="{ 'selected-row': selectedTrace?.sessionId === trace.sessionId }"
              @click="selectTrace(trace)"
            >
              <td>
                <span class="mono truncate" style="max-width:140px; display:inline-block;">
                  {{ trace.sessionId }}
                </span>
              </td>
              <td>
                <span class="badge badge-info">{{ trace.agent }}</span>
              </td>
              <td class="mono">{{ trace.tokensUsed.toLocaleString() }}</td>
              <td class="mono">{{ trace.maxTokens.toLocaleString() }}</td>
              <td>
                <div class="pct-cell">
                  <div class="pct-bar-wrap">
                    <div
                      class="pct-bar-fill"
                      :class="pctClass(trace.tokensUsed, trace.maxTokens)"
                      :style="{ width: pct(trace.tokensUsed, trace.maxTokens) + '%' }"
                    ></div>
                  </div>
                  <span class="mono" :class="pctTextClass(trace.tokensUsed, trace.maxTokens)">
                    {{ pct(trace.tokensUsed, trace.maxTokens) }}%
                  </span>
                </div>
              </td>
              <td class="mono">{{ trace.messages }}</td>
              <td class="text-muted" style="font-size:.8rem;">{{ trace.lastActivity }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <div v-if="selectedTrace" class="card detail-panel">
        <div class="flex items-center justify-between" style="margin-bottom:.75rem;">
          <div>
            <div class="detail-title">Message History</div>
            <div class="mono text-muted" style="font-size:.7rem;">{{ selectedTrace.sessionId }}</div>
          </div>
          <button class="btn btn-ghost" style="font-size:.75rem;" @click="selectedTrace = null">✕ Close</button>
        </div>

        <div class="messages-list">
          <div
            v-for="(msg, i) in selectedTrace.messageList"
            :key="i"
            class="message-item"
            :class="'role-' + msg.role"
          >
            <div class="msg-header flex items-center justify-between">
              <span class="msg-role">{{ msg.role }}</span>
              <span class="mono text-muted" style="font-size:.7rem;">{{ msg.tokens }} tokens</span>
            </div>
            <div class="msg-content">{{ msg.content }}</div>
          </div>
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

interface Message { role: string; content: string; tokens: number }
interface Trace {
  sessionId: string
  agent: string
  tokensUsed: number
  maxTokens: number
  messages: number
  lastActivity: string
  messageList: Message[]
}

const loading = ref(false)
const traces = ref<Trace[]>([])
const selectedTrace = ref<Trace | null>(null)

const demoTraces: Trace[] = [
  {
    sessionId: 'sess-a1b2c3d4', agent: 'PlannerAgent', tokensUsed: 42300, maxTokens: 128000, messages: 14, lastActivity: '2 min ago',
    messageList: [
      { role: 'system', content: 'You are a planning agent responsible for decomposing complex tasks...', tokens: 312 },
      { role: 'user', content: 'Please analyze the current backlog and generate a sprint plan.', tokens: 45 },
      { role: 'assistant', content: 'I will analyze the backlog items and create an optimized sprint plan based on priority and capacity...', tokens: 198 },
    ]
  },
  {
    sessionId: 'sess-e5f6g7h8', agent: 'RAGAgent', tokensUsed: 89600, maxTokens: 128000, messages: 28, lastActivity: '5 min ago',
    messageList: [
      { role: 'system', content: 'You are a retrieval-augmented generation agent...', tokens: 256 },
      { role: 'user', content: 'What are the compliance requirements for GDPR data handling?', tokens: 52 },
    ]
  },
  {
    sessionId: 'sess-i9j0k1l2', agent: 'ExecutorAgent', tokensUsed: 12800, maxTokens: 32000, messages: 6, lastActivity: '1 min ago',
    messageList: [
      { role: 'system', content: 'You are an execution agent that runs code and tools...', tokens: 180 },
      { role: 'user', content: 'Run the database migration script', tokens: 28 },
    ]
  },
  {
    sessionId: 'sess-m3n4o5p6', agent: 'MonitorAgent', tokensUsed: 27500, maxTokens: 32000, messages: 11, lastActivity: '30 sec ago',
    messageList: [
      { role: 'system', content: 'You are a monitoring agent watching system health metrics...', tokens: 210 },
    ]
  },
  {
    sessionId: 'sess-q7r8s9t0', agent: 'PlannerAgent', tokensUsed: 5200, maxTokens: 128000, messages: 3, lastActivity: '10 min ago',
    messageList: [
      { role: 'system', content: 'You are a planning agent...', tokens: 312 },
      { role: 'user', content: 'Create a capacity plan for next quarter.', tokens: 38 },
    ]
  },
]

function pct(used: number, max: number) { return Math.min(100, Math.round((used / max) * 100)) }
function pctClass(used: number, max: number) {
  const p = pct(used, max)
  if (p < 60) return 'pct-ok'
  if (p < 85) return 'pct-warn'
  return 'pct-err'
}
function pctTextClass(used: number, max: number) {
  const p = pct(used, max)
  if (p < 60) return 'text-ok'
  if (p < 85) return 'text-warn'
  return 'text-danger'
}

function selectTrace(trace: Trace) {
  selectedTrace.value = selectedTrace.value?.sessionId === trace.sessionId ? null : trace
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/context/traces')
    traces.value = res.traces ?? res
  } catch {
    traces.value = demoTraces
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.context-traces-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.layout { display: flex; flex-direction: column; gap: 1rem; }
.layout.with-detail { display: grid; grid-template-columns: 1fr 380px; gap: 1rem; align-items: start; }

.table-panel { }
.detail-panel { padding: 1rem; }
.detail-title { font-weight: 700; font-size: .95rem; }

.clickable-row { cursor: pointer; transition: background .15s; }
.clickable-row:hover td { background: var(--bg3); }
.selected-row td { background: color-mix(in srgb, var(--accent) 10%, transparent); }

.pct-cell { display: flex; align-items: center; gap: .5rem; }
.pct-bar-wrap { width: 80px; height: 4px; background: var(--bg3); border-radius: 2px; flex-shrink: 0; }
.pct-bar-fill { height: 100%; border-radius: 2px; transition: width .4s; }
.pct-ok { background: var(--accent3); }
.pct-warn { background: var(--warn); }
.pct-err { background: var(--danger); }

.messages-list { display: flex; flex-direction: column; gap: .5rem; max-height: 500px; overflow-y: auto; }
.message-item { border-radius: var(--radius-sm); padding: .6rem .75rem; font-size: .82rem; }
.role-system { background: color-mix(in srgb, var(--accent) 8%, var(--bg2)); border-left: 3px solid var(--accent); }
.role-user { background: color-mix(in srgb, var(--accent2) 8%, var(--bg2)); border-left: 3px solid var(--accent2); }
.role-assistant { background: color-mix(in srgb, var(--accent3) 8%, var(--bg2)); border-left: 3px solid var(--accent3); }
.msg-header { margin-bottom: .3rem; }
.msg-role { font-weight: 700; font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; color: var(--text-muted); }
.msg-content { color: var(--text); line-height: 1.5; }
</style>
