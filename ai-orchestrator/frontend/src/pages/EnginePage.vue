<template>
  <div class="engine-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">⚙ Engine Room</h1>
      <div class="flex gap-2">
        <span class="badge" :class="health.mode === 'serve' ? 'badge-ok' : health.mode === 'subprocess' ? 'badge-warn' : 'badge-err'">
          OpenCode: {{ health.mode ?? '…' }}
        </span>
        <button class="btn btn-ghost" @click="refresh">↻ Refresh</button>
        <button class="btn btn-primary" @click="openNewSession">+ New Session</button>
      </div>
    </div>

    <!-- Metrics row -->
    <div class="kpi-row">
      <div class="card kpi-card">
        <div class="kpi-val text-accent">{{ sessions.length }}</div>
        <div class="kpi-label text-muted">Active Sessions</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-val">{{ totalTokens.toLocaleString() }}</div>
        <div class="kpi-label text-muted">Total Tokens</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-val text-ok">${{ totalCost.toFixed(4) }}</div>
        <div class="kpi-label text-muted">Est. Cost</div>
      </div>
      <div class="card kpi-card">
        <div class="kpi-val">{{ totalFiles }}</div>
        <div class="kpi-label text-muted">Files Modified</div>
      </div>
    </div>

    <!-- Main layout -->
    <div class="engine-layout">
      <!-- Sessions list -->
      <div class="card sessions-panel">
        <div class="panel-title">Active Sessions</div>
        <div v-if="sessions.length === 0" class="empty-state text-muted">
          No active coding sessions
        </div>
        <div
          v-for="s in sessions"
          :key="s.session_id"
          class="session-item"
          :class="{ active: activeSession?.session_id === s.session_id }"
          @click="selectSession(s)"
        >
          <div class="si-header flex justify-between">
            <span class="si-id mono">{{ s.session_id }}</span>
            <span class="badge badge-info" style="font-size:9px">{{ s.message_count }}msg</span>
          </div>
          <div class="si-task text-muted">task: {{ s.task_id }}</div>
          <div class="si-files text-muted" v-if="s.files_modified?.length">
            {{ s.files_modified.length }} file(s) changed
          </div>
          <div class="si-tokens mono text-muted">
            ↑{{ s.tokens_in }} ↓{{ s.tokens_out }}
          </div>
        </div>
      </div>

      <!-- Coding console -->
      <div class="card console-panel">
        <div v-if="!activeSession && !oneshot" class="console-empty">
          <div style="font-size:32px;opacity:0.2">⚙</div>
          <div class="text-muted">Select a session or start a one-shot task</div>
          <button class="btn btn-primary mt-2" @click="openOneshot">▶ One-Shot Task</button>
        </div>

        <!-- One-shot mode -->
        <div v-else-if="oneshot" class="oneshot-panel">
          <div class="panel-title">One-Shot Coding Task</div>
          <textarea
            v-model="oneshotPrompt"
            class="prompt-box"
            placeholder="Describe the coding task… e.g. 'Add input validation to the /api/v1/tasks POST endpoint'"
            rows="6"
          ></textarea>
          <div class="flex gap-2 mt-2 items-center">
            <select v-model="oneshotProvider" class="select-sm">
              <option value="anthropic">Anthropic</option>
              <option value="ollama">Ollama (local)</option>
            </select>
            <select v-model="oneshotModel" class="select-sm">
              <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
              <option value="claude-opus-4-6">claude-opus-4-6</option>
              <option value="qwen2.5-coder:14b">qwen2.5-coder:14b</option>
            </select>
            <button class="btn btn-ghost" @click="oneshot = false">✕ Cancel</button>
            <button class="btn btn-primary" :disabled="running || !oneshotPrompt" @click="runOneshot">
              {{ running ? '⏳ Running…' : '▶ Execute' }}
            </button>
          </div>
          <div v-if="oneshotResult" class="result-box mt-3">
            <div class="result-header flex justify-between">
              <span class="text-ok" v-if="oneshotResult.success">✓ Success</span>
              <span class="text-danger" v-else>✗ Failed</span>
              <span class="text-muted mono" style="font-size:10px">
                {{ oneshotResult.backend_used }} · ${{ oneshotResult.cost_usd }}
              </span>
            </div>
            <div v-if="oneshotResult.files_modified?.length" class="result-files text-muted mt-1">
              Files: {{ oneshotResult.files_modified.join(', ') }}
            </div>
            <pre class="result-summary mono">{{ oneshotResult.summary }}</pre>
            <div v-if="oneshotResult.diff" class="diff-box mt-2">
              <div class="panel-title mb-1">Diff</div>
              <pre class="diff-pre">{{ oneshotResult.diff }}</pre>
            </div>
          </div>
        </div>

        <!-- Session chat mode -->
        <template v-else-if="activeSession">
          <div class="session-header flex justify-between items-center mb-2">
            <div>
              <div class="mono" style="font-size:11px;color:var(--accent)">{{ activeSession.session_id }}</div>
              <div class="text-muted" style="font-size:10px">task: {{ activeSession.task_id }}</div>
            </div>
            <div class="flex gap-2">
              <button class="btn btn-ghost" style="font-size:11px" @click="fetchDiff">⊞ Diff</button>
              <button class="btn btn-danger" style="font-size:11px" @click="closeSession">✕ Close</button>
            </div>
          </div>

          <div class="messages-area" ref="messagesEl">
            <div v-for="(m, i) in chatMessages" :key="i" class="message" :class="m.role">
              <div class="msg-role">{{ m.role === 'user' ? 'You' : 'OpenCode' }}</div>
              <pre class="msg-content mono">{{ m.content }}</pre>
            </div>
            <div v-if="streaming" class="message assistant streaming">
              <div class="msg-role">OpenCode</div>
              <pre class="msg-content mono">{{ streamBuffer }}<span class="cursor">▌</span></pre>
            </div>
          </div>

          <div v-if="diffContent" class="diff-box mt-2">
            <div class="flex justify-between items-center mb-1">
              <div class="panel-title">Diff</div>
              <button class="btn btn-ghost" style="font-size:10px" @click="diffContent = ''">✕</button>
            </div>
            <pre class="diff-pre">{{ diffContent }}</pre>
          </div>

          <div class="chat-input-row flex gap-2 mt-2">
            <textarea
              v-model="chatInput"
              class="chat-input"
              placeholder="Send a message to OpenCode…"
              rows="2"
              @keydown.enter.ctrl.exact.prevent="sendMessage"
            ></textarea>
            <button class="btn btn-primary" :disabled="streaming || !chatInput" @click="sendMessage">
              {{ streaming ? '…' : '▶' }}
            </button>
          </div>
          <div class="text-muted" style="font-size:10px;margin-top:4px">Ctrl+Enter to send</div>
        </template>
      </div>
    </div>

    <!-- New Session Modal -->
    <div v-if="newSessionModal" class="modal-overlay" @click.self="newSessionModal = false">
      <div class="modal-box card">
        <h3 class="modal-title">New OpenCode Session</h3>
        <div class="form-grid mt-3">
          <label><span class="form-label">Task ID</span><input v-model="newForm.task_id" placeholder="task-xyz" /></label>
          <label><span class="form-label">Provider</span>
            <select v-model="newForm.provider">
              <option value="anthropic">Anthropic (Claude)</option>
              <option value="ollama">Ollama (local GPU)</option>
            </select>
          </label>
          <label><span class="form-label">Model</span>
            <select v-model="newForm.model">
              <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
              <option value="claude-opus-4-6">claude-opus-4-6</option>
              <option value="qwen2.5-coder:14b">qwen2.5-coder:14b</option>
            </select>
          </label>
        </div>
        <div class="flex gap-2 justify-end mt-4">
          <button class="btn btn-ghost" @click="newSessionModal = false">Cancel</button>
          <button class="btn btn-primary" @click="createSession">Create</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { apiV5: api, apiV5f: apif } = useApi()
const app = useAppStore()

interface SessionInfo {
  session_id: string
  task_id: string
  tenant_id: string
  status: string
  files_modified: string[]
  tokens_in: number
  tokens_out: number
  message_count: number
}

interface OneshotResult {
  session_id: string
  summary: string
  files_modified: string[]
  diff: string
  tokens_in: number
  tokens_out: number
  cost_usd: number
  backend_used: string
  success: boolean
  error: string
}

interface ChatMessage { role: 'user' | 'assistant'; content: string }

const health = ref<{ mode?: string; status?: string }>({})
const sessions = ref<SessionInfo[]>([])
const activeSession = ref<SessionInfo | null>(null)
const chatMessages = ref<ChatMessage[]>([])
const chatInput = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const diffContent = ref('')
const messagesEl = ref<HTMLElement | null>(null)
const oneshot = ref(false)
const oneshotPrompt = ref('')
const oneshotProvider = ref('anthropic')
const oneshotModel = ref('claude-sonnet-4-6')
const oneshotResult = ref<OneshotResult | null>(null)
const running = ref(false)
const newSessionModal = ref(false)
const newForm = ref({ task_id: `session-${Date.now()}`, provider: 'anthropic', model: 'claude-sonnet-4-6' })

const totalTokens = computed(() => sessions.value.reduce((a, s) => a + s.tokens_in + s.tokens_out, 0))
const totalCost = computed(() => sessions.value.reduce((a, s) => a + (s.tokens_in * 3 + s.tokens_out * 15) / 1_000_000, 0))
const totalFiles = computed(() => sessions.value.reduce((a, s) => a + (s.files_modified?.length ?? 0), 0))

function openNewSession() { newSessionModal.value = true }
function openOneshot() { oneshot.value = true; oneshotResult.value = null; activeSession.value = null }

async function refresh() {
  try {
    const [h, s] = await Promise.all([
      api<any>('/opencode/health'),
      api<{ sessions: SessionInfo[] }>('/opencode/sessions'),
    ])
    health.value = h
    sessions.value = s.sessions ?? []
  } catch {
    health.value = { mode: 'unavailable', status: 'error' }
  }
}

function selectSession(s: SessionInfo) {
  activeSession.value = s
  oneshot.value = false
  chatMessages.value = []
  diffContent.value = ''
}

async function createSession() {
  try {
    const r = await apif<any>('/opencode/sessions', {
      task_id: newForm.value.task_id,
      provider: newForm.value.provider,
      model: newForm.value.model,
      tenant_id: app.tenantId,
    })
    newSessionModal.value = false
    app.showToast(`Session ${r.session_id} created`, 'ok')
    await refresh()
    const s = sessions.value.find(x => x.session_id === r.session_id)
    if (s) selectSession(s)
  } catch { app.showToast('Failed to create session', 'err') }
}

async function sendMessage() {
  if (!activeSession.value || !chatInput.value.trim()) return
  const msg = chatInput.value.trim()
  chatInput.value = ''
  chatMessages.value.push({ role: 'user', content: msg })
  streaming.value = true
  streamBuffer.value = ''

  try {
    const response = await fetch(
      `/api/v5/opencode/sessions/${activeSession.value.session_id}/message`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'X-Tenant-Id': app.tenantId },
        body: JSON.stringify({ message: msg }),
      }
    )
    const reader = response.body!.getReader()
    const decoder = new TextDecoder()
    let buffer = ''

    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() ?? ''
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue
        const raw = line.slice(6)
        if (raw === '[DONE]') break
        try {
          const data = JSON.parse(raw)
          if (data.token) streamBuffer.value += data.token
          if (data.done) {
            chatMessages.value.push({ role: 'assistant', content: streamBuffer.value })
            streamBuffer.value = ''
            await refresh()
          }
        } catch { /* skip malformed */ }
      }
    }
  } catch (e: any) {
    app.showToast(`Stream error: ${e.message}`, 'err')
  } finally {
    if (streamBuffer.value) {
      chatMessages.value.push({ role: 'assistant', content: streamBuffer.value })
      streamBuffer.value = ''
    }
    streaming.value = false
    await nextTick()
    messagesEl.value?.scrollTo({ top: messagesEl.value.scrollHeight, behavior: 'smooth' })
  }
}

async function fetchDiff() {
  if (!activeSession.value) return
  try {
    const r = await api<{ diff: string }>(`/opencode/sessions/${activeSession.value.session_id}`)
    diffContent.value = (r as any).diff ?? 'No diff available'
  } catch { app.showToast('Could not fetch diff', 'err') }
}

async function closeSession() {
  if (!activeSession.value) return
  try {
    await fetch(`/api/v5/opencode/sessions/${activeSession.value.session_id}`, {
      method: 'DELETE',
      headers: { 'X-Tenant-Id': app.tenantId },
    })
    app.showToast('Session closed', 'ok')
    activeSession.value = null
    await refresh()
  } catch { app.showToast('Close failed', 'err') }
}

async function runOneshot() {
  if (!oneshotPrompt.value.trim()) return
  running.value = true
  oneshotResult.value = null
  try {
    const r = await apif<OneshotResult>('/opencode/run', {
      prompt: oneshotPrompt.value,
      provider: oneshotProvider.value,
      model: oneshotModel.value,
      tenant_id: app.tenantId,
    })
    oneshotResult.value = r
    app.showToast(r.success ? 'Task complete' : 'Task failed', r.success ? 'ok' : 'err')
  } catch (e: any) {
    app.showToast(`Error: ${e.message}`, 'err')
  } finally {
    running.value = false
  }
}

onMounted(refresh)
</script>

<style scoped>
.engine-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }

.kpi-row { display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; }
.kpi-card { text-align: center; padding: 14px; }
.kpi-val { font-size: 26px; font-weight: 800; font-family: var(--font-mono); }
.kpi-label { font-size: 11px; margin-top: 3px; }

.engine-layout { display: grid; grid-template-columns: 280px 1fr; gap: 12px; min-height: 500px; }

.sessions-panel { padding: 12px; overflow-y: auto; max-height: calc(100vh - 300px); }
.panel-title { font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 10px; }

.empty-state { text-align: center; padding: 40px 0; font-size: 12px; }

.session-item { padding: 10px; border-radius: var(--radius-sm); border: 1px solid transparent; cursor: pointer; margin-bottom: 6px; transition: var(--transition); }
.session-item:hover { background: var(--bg3); }
.session-item.active { background: rgba(0,212,255,0.06); border-color: rgba(0,212,255,0.2); }
.si-id { font-size: 11px; color: var(--accent); }
.si-task { font-size: 10px; margin-top: 2px; }
.si-files { font-size: 10px; }
.si-tokens { font-size: 9px; margin-top: 3px; }

.console-panel { padding: 16px; display: flex; flex-direction: column; }
.console-empty { flex: 1; display: flex; flex-direction: column; align-items: center; justify-content: center; gap: 12px; }

/* Messages */
.messages-area { flex: 1; overflow-y: auto; max-height: 350px; display: flex; flex-direction: column; gap: 10px; padding-right: 4px; }
.message { padding: 10px; border-radius: var(--radius-sm); }
.message.user { background: rgba(0,212,255,0.06); border: 1px solid rgba(0,212,255,0.15); }
.message.assistant { background: var(--bg3); border: 1px solid var(--panel-border); }
.message.streaming { border-color: rgba(0,212,255,0.3); }
.msg-role { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); margin-bottom: 5px; }
.msg-content { font-size: 12.5px; white-space: pre-wrap; word-break: break-word; margin: 0; }
.cursor { animation: blink 1s step-end infinite; }
@keyframes blink { 50% { opacity: 0; } }

/* Chat input */
.chat-input-row { align-items: flex-end; }
.chat-input { flex: 1; background: var(--bg3); border: 1px solid var(--panel-border); border-radius: var(--radius-sm); padding: 8px; color: var(--text); font-size: 13px; resize: none; font-family: var(--font-mono); }
.chat-input:focus { outline: none; border-color: var(--accent); }

/* One-shot */
.oneshot-panel { display: flex; flex-direction: column; gap: 0; }
.prompt-box { background: var(--bg3); border: 1px solid var(--panel-border); border-radius: var(--radius-sm); padding: 10px; color: var(--text); font-size: 13px; width: 100%; resize: vertical; font-family: inherit; }
.prompt-box:focus { outline: none; border-color: var(--accent); }
.select-sm { background: var(--bg3); border: 1px solid var(--panel-border); border-radius: var(--radius-sm); padding: 5px 8px; color: var(--text); font-size: 12px; }
.result-box { background: var(--bg3); border: 1px solid var(--panel-border); border-radius: var(--radius-sm); padding: 12px; }
.result-header { font-size: 12px; font-weight: 600; }
.result-files { font-size: 11px; }
.result-summary { font-size: 12px; white-space: pre-wrap; word-break: break-word; margin: 8px 0 0; max-height: 200px; overflow-y: auto; }

/* Diff */
.diff-box { background: var(--bg); border: 1px solid var(--panel-border); border-radius: var(--radius-sm); padding: 10px; }
.diff-pre { font-size: 11px; white-space: pre; overflow-x: auto; margin: 0; max-height: 200px; overflow-y: auto; color: var(--text-muted); }

/* Session header */
.session-header { padding-bottom: 8px; border-bottom: 1px solid var(--panel-border); }

/* Modal */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 500; backdrop-filter: blur(4px); }
.modal-box { width: 420px; padding: 20px; }
.modal-title { font-size: 15px; font-weight: 700; }
.form-grid { display: flex; flex-direction: column; gap: 10px; }
.form-label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }

.mt-2 { margin-top: 8px; }
.mt-3 { margin-top: 12px; }
.mb-1 { margin-bottom: 4px; }

@media (max-width: 900px) { .engine-layout { grid-template-columns: 1fr; } .kpi-row { grid-template-columns: repeat(2, 1fr); } }
</style>
