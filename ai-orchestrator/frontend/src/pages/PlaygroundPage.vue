<template>
  <div class="playground-page">
    <div class="page-header">
      <h1 class="page-title">API Playground</h1>
      <p class="text-muted">Build and test API requests interactively</p>
    </div>

    <div class="playground-layout">
      <!-- Request Builder -->
      <div class="card request-panel">
        <div class="panel-title">Request Builder</div>

        <div class="flex gap-2 items-center" style="margin-bottom:.75rem;">
          <select class="method-select" v-model="method" :class="'method-' + method">
            <option>GET</option>
            <option>POST</option>
            <option>PUT</option>
            <option>DELETE</option>
          </select>
          <input
            class="url-input"
            v-model="url"
            placeholder="/llm/status"
            list="endpoint-suggestions"
            @keydown.enter="execute"
          />
          <datalist id="endpoint-suggestions">
            <option v-for="ep in commonEndpoints" :key="ep" :value="ep" />
          </datalist>
          <button class="btn btn-primary" @click="execute" :disabled="executing">
            {{ executing ? 'Sending...' : 'Send' }}
          </button>
        </div>

        <div class="tabs flex gap-2" style="margin-bottom:.5rem;">
          <button
            v-for="tab in ['Body', 'Headers']"
            :key="tab"
            class="tab-btn"
            :class="{ active: activeTab === tab }"
            @click="activeTab = tab"
          >{{ tab }}</button>
        </div>

        <div v-if="activeTab === 'Body'">
          <div class="text-muted" style="font-size:.75rem; margin-bottom:.25rem;">
            JSON Body {{ method === 'GET' || method === 'DELETE' ? '(not applicable for ' + method + ')' : '' }}
          </div>
          <textarea
            class="code-textarea"
            v-model="body"
            :disabled="method === 'GET' || method === 'DELETE'"
            placeholder='{"key": "value"}'
            rows="8"
          ></textarea>
        </div>
        <div v-else>
          <div class="text-muted" style="font-size:.75rem; margin-bottom:.25rem;">Headers (JSON)</div>
          <textarea
            class="code-textarea"
            v-model="headers"
            placeholder='{"Authorization": "Bearer ..."}'
            rows="8"
          ></textarea>
        </div>
      </div>

      <!-- Response Viewer -->
      <div class="card response-panel">
        <div class="panel-title flex items-center justify-between">
          <span>Response</span>
          <div v-if="lastResponse" class="flex gap-2 items-center">
            <span class="badge" :class="statusBadge(lastResponse.status)">{{ lastResponse.status }}</span>
            <span class="badge badge-info">{{ lastResponse.time }}ms</span>
          </div>
        </div>
        <div v-if="!lastResponse" class="empty-state text-muted">
          Send a request to see the response here
        </div>
        <pre v-else class="response-json">{{ lastResponse.body }}</pre>
      </div>
    </div>

    <!-- History -->
    <div class="card" style="margin-top:1rem;">
      <div class="panel-title flex items-center justify-between">
        <span>Request History (last 10)</span>
        <button v-if="history.length" class="btn btn-ghost" style="font-size:.75rem;" @click="history = []">Clear</button>
      </div>
      <div v-if="!history.length" class="text-muted" style="padding:.75rem; font-size:.85rem;">No requests yet</div>
      <table v-else class="tbl">
        <thead>
          <tr><th>Method</th><th>URL</th><th>Status</th><th>Time</th><th>At</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="(h, i) in history" :key="i">
            <td><span class="badge" :class="'method-badge-' + h.method">{{ h.method }}</span></td>
            <td class="mono" style="font-size:.8rem;">{{ h.url }}</td>
            <td><span class="badge" :class="statusBadge(h.status)">{{ h.status }}</span></td>
            <td class="mono text-muted" style="font-size:.8rem;">{{ h.time }}ms</td>
            <td class="text-muted" style="font-size:.75rem;">{{ h.at }}</td>
            <td><button class="btn btn-ghost" style="font-size:.7rem; padding:.2rem .4rem;" @click="replayRequest(h)">Replay</button></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { apif } = useApi()
const store = useAppStore()

const method = ref('GET')
const url = ref('/llm/status')
const body = ref('')
const headers = ref('')
const activeTab = ref('Body')
const executing = ref(false)

interface Response { status: number; body: string; time: number }
interface HistoryItem { method: string; url: string; status: number; time: number; at: string; body: string; headers: string }

const lastResponse = ref<Response | null>(null)
const history = ref<HistoryItem[]>([])

const commonEndpoints = [
  '/llm/status', '/token-budgets', '/context/traces', '/entropy/metrics',
  '/memory/list', '/knowledge/list', '/data-lineage', '/compliance/report',
  '/health/grid', '/usage/analytics', '/deployments/blue-green/status',
  '/capacity/predict', '/system/changelog', '/connect/integrations',
  '/twin/status', '/gates',
]

async function execute() {
  if (!url.value) return
  executing.value = true
  const start = Date.now()
  try {
    let parsedHeaders: Record<string, string> = {}
    try { parsedHeaders = headers.value ? JSON.parse(headers.value) : {} } catch {}

    const opts: RequestInit = {
      method: method.value,
      headers: { 'Content-Type': 'application/json', ...parsedHeaders }
    }
    if ((method.value === 'POST' || method.value === 'PUT') && body.value) {
      opts.body = body.value
    }

    const res = await apif<any>(url.value, opts)
    const elapsed = Date.now() - start
    let responseBody = ''
    try { responseBody = JSON.stringify(await res.json(), null, 2) } catch { responseBody = await res.text() }
    lastResponse.value = { status: res.status, body: responseBody, time: elapsed }
    pushHistory(res.status, elapsed)
  } catch (e: any) {
    const elapsed = Date.now() - start
    lastResponse.value = { status: 0, body: `Error: ${e.message}`, time: elapsed }
    pushHistory(0, elapsed)
  } finally {
    executing.value = false
  }
}

function pushHistory(status: number, time: number) {
  history.value.unshift({
    method: method.value,
    url: url.value,
    status,
    time,
    at: new Date().toLocaleTimeString(),
    body: body.value,
    headers: headers.value
  })
  if (history.value.length > 10) history.value = history.value.slice(0, 10)
}

function replayRequest(h: HistoryItem) {
  method.value = h.method
  url.value = h.url
  body.value = h.body
  headers.value = h.headers
  execute()
}

function statusBadge(status: number) {
  if (status >= 200 && status < 300) return 'badge-ok'
  if (status >= 400 && status < 500) return 'badge-warn'
  if (status >= 500) return 'badge-err'
  return 'badge-info'
}
</script>

<style scoped>
.playground-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.playground-layout {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 1rem;
  margin-bottom: 0;
}

.request-panel, .response-panel { padding: 1rem; }
.panel-title { font-weight: 700; font-size: .9rem; margin-bottom: .75rem; }

.method-select {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .4rem .5rem;
  font-family: var(--font-mono);
  font-size: .85rem;
  font-weight: 700;
  cursor: pointer;
  width: 90px;
  flex-shrink: 0;
}
.method-GET { color: var(--accent3); }
.method-POST { color: var(--accent); }
.method-PUT { color: var(--warn); }
.method-DELETE { color: var(--danger); }

.url-input {
  flex: 1;
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .4rem .65rem;
  font-family: var(--font-mono);
  font-size: .85rem;
  outline: none;
  transition: var(--transition);
}
.url-input:focus { border-color: var(--accent); }

.tab-btn {
  background: none;
  border: none;
  color: var(--text-muted);
  padding: .3rem .6rem;
  cursor: pointer;
  font-size: .82rem;
  border-bottom: 2px solid transparent;
  transition: var(--transition);
}
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }

.code-textarea {
  width: 100%;
  background: var(--bg);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: .82rem;
  padding: .65rem;
  resize: vertical;
  outline: none;
  line-height: 1.5;
  box-sizing: border-box;
}
.code-textarea:focus { border-color: var(--accent); }
.code-textarea:disabled { opacity: .4; cursor: not-allowed; }

.empty-state { padding: 2rem; text-align: center; font-size: .9rem; }
.response-json {
  font-family: var(--font-mono);
  font-size: .8rem;
  line-height: 1.6;
  color: var(--accent3);
  background: var(--bg);
  border-radius: var(--radius-sm);
  padding: .75rem;
  overflow: auto;
  max-height: 400px;
  white-space: pre-wrap;
  word-break: break-all;
}

.method-badge-GET { background: color-mix(in srgb, var(--accent3) 15%, transparent); color: var(--accent3); border-color: var(--accent3); }
.method-badge-POST { background: color-mix(in srgb, var(--accent) 15%, transparent); color: var(--accent); border-color: var(--accent); }
.method-badge-PUT { background: color-mix(in srgb, var(--warn) 15%, transparent); color: var(--warn); border-color: var(--warn); }
.method-badge-DELETE { background: color-mix(in srgb, var(--danger) 15%, transparent); color: var(--danger); border-color: var(--danger); }
</style>
