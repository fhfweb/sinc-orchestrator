<template>
  <div class="sdk-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">SDK Generator</h1>
        <p class="text-muted">Generate client code for any language and endpoint selection</p>
      </div>
    </div>

    <div class="sdk-layout">
      <!-- Config Panel -->
      <div class="card config-panel">
        <div class="panel-title">Configuration</div>

        <!-- Language Selector -->
        <div class="field-group">
          <div class="field-label text-muted">Language</div>
          <div class="lang-buttons flex gap-2">
            <button
              v-for="lang in languages"
              :key="lang.id"
              class="btn"
              :class="selectedLang === lang.id ? 'btn-primary' : 'btn-ghost'"
              @click="selectedLang = lang.id; generateCode()"
            >{{ lang.label }}</button>
          </div>
        </div>

        <!-- Endpoint Checkboxes -->
        <div class="field-group">
          <div class="field-label text-muted">
            Endpoints
            <button class="btn btn-ghost" style="font-size:.7rem; padding:.15rem .4rem; margin-left:.5rem;" @click="toggleAll">
              {{ allSelected ? 'None' : 'All' }}
            </button>
          </div>
          <div class="endpoint-list">
            <label
              v-for="ep in endpoints"
              :key="ep.path"
              class="endpoint-item"
            >
              <input type="checkbox" :value="ep.path" v-model="selectedEndpoints" @change="generateCode()" />
              <span class="mono ep-path">{{ ep.path }}</span>
              <span class="badge" :class="methodBadge(ep.method)" style="font-size:.65rem;">{{ ep.method }}</span>
            </label>
          </div>
        </div>

        <button class="btn btn-primary" style="width:100%;" @click="fetchCode" :disabled="generating">
          {{ generating ? 'Generating...' : 'Generate from API' }}
        </button>
      </div>

      <!-- Code Panel -->
      <div class="card code-panel">
        <div class="code-panel-header flex items-center justify-between">
          <div class="flex items-center gap-2">
            <span class="panel-title" style="margin-bottom:0;">Generated Code</span>
            <span class="badge badge-info">{{ selectedLang }}</span>
          </div>
          <div class="flex gap-2">
            <button class="btn btn-ghost" style="font-size:.78rem;" @click="copyCode">Copy</button>
            <button class="btn btn-ghost" style="font-size:.78rem;" @click="downloadCode">Download</button>
          </div>
        </div>
        <pre class="code-block"><code>{{ generatedCode }}</code></pre>
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

const generating = ref(false)
const selectedLang = ref('python')
const selectedEndpoints = ref<string[]>(['/llm/status', '/token-budgets', '/health/grid'])
const generatedCode = ref('')

const languages = [
  { id: 'python', label: 'Python' },
  { id: 'typescript', label: 'TypeScript' },
  { id: 'go', label: 'Go' },
  { id: 'curl', label: 'cURL' },
]

const endpoints = [
  { path: '/llm/status', method: 'GET' },
  { path: '/token-budgets', method: 'GET' },
  { path: '/context/traces', method: 'GET' },
  { path: '/entropy/metrics', method: 'GET' },
  { path: '/memory/list', method: 'GET' },
  { path: '/memory/prune', method: 'POST' },
  { path: '/knowledge/list', method: 'GET' },
  { path: '/knowledge/save', method: 'POST' },
  { path: '/compliance/report', method: 'GET' },
  { path: '/health/grid', method: 'GET' },
  { path: '/usage/analytics', method: 'GET' },
  { path: '/deployments/blue-green/status', method: 'GET' },
  { path: '/capacity/predict', method: 'GET' },
  { path: '/gates', method: 'GET' },
  { path: '/twin/status', method: 'GET' },
]

const allSelected = computed(() => selectedEndpoints.value.length === endpoints.length)
function toggleAll() {
  selectedEndpoints.value = allSelected.value ? [] : endpoints.map(e => e.path)
  generateCode()
}

function generateCode() {
  const BASE = 'https://your-host/api/v5/dashboard'
  const eps = selectedEndpoints.value

  if (selectedLang.value === 'python') {
    generatedCode.value = `import requests

BASE_URL = "${BASE}"
API_KEY = "your-api-key"
HEADERS = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}

${eps.map(ep => {
  const found = endpoints.find(e => e.path === ep)
  const fn = ep.replace(/\//g, '_').replace(/^_/, '').replace(/-/g, '_')
  const method = found?.method ?? 'GET'
  return `def ${fn}():
    resp = requests.${method.toLowerCase()}(f"{BASE_URL}${ep}", headers=HEADERS)
    resp.raise_for_status()
    return resp.json()`
}).join('\n\n')}
`
  } else if (selectedLang.value === 'typescript') {
    generatedCode.value = `const BASE_URL = "${BASE}";
const API_KEY = "your-api-key";
const headers = { Authorization: \`Bearer \${API_KEY}\`, "Content-Type": "application/json" };

${eps.map(ep => {
  const found = endpoints.find(e => e.path === ep)
  const fn = ep.replace(/\//g, '_').replace(/^_/, '').replace(/-/g, '_')
  const method = found?.method ?? 'GET'
  return `export async function ${fn}(): Promise<any> {
  const res = await fetch(\`\${BASE_URL}${ep}\`, { method: "${method}", headers });
  if (!res.ok) throw new Error(\`HTTP \${res.status}\`);
  return res.json();
}`
}).join('\n\n')}
`
  } else if (selectedLang.value === 'go') {
    generatedCode.value = `package noc

import (
\t"encoding/json"
\t"fmt"
\t"net/http"
)

const baseURL = "${BASE}"
const apiKey = "your-api-key"

func doRequest(method, path string) (map[string]interface{}, error) {
\treq, _ := http.NewRequest(method, baseURL+path, nil)
\treq.Header.Set("Authorization", "Bearer "+apiKey)
\tclient := &http.Client{}
\tresp, err := client.Do(req)
\tif err != nil { return nil, err }
\tdefer resp.Body.Close()
\tvar result map[string]interface{}
\tjson.NewDecoder(resp.Body).Decode(&result)
\treturn result, nil
}

${eps.map(ep => {
  const fn = ep.split('/').filter(Boolean).map(s => s.charAt(0).toUpperCase() + s.slice(1).replace(/-([a-z])/g, (_, c) => c.toUpperCase())).join('')
  const found = endpoints.find(e => e.path === ep)
  return `func ${fn}() (map[string]interface{}, error) {
\treturn doRequest("${found?.method ?? 'GET'}", "${ep}")
}`
}).join('\n\n')}
`
  } else {
    generatedCode.value = eps.map(ep => {
      const found = endpoints.find(e => e.path === ep)
      return `# ${ep}
curl -X ${found?.method ?? 'GET'} "${BASE}${ep}" \\
  -H "Authorization: Bearer your-api-key" \\
  -H "Content-Type: application/json"`
    }).join('\n\n')
  }
}

async function fetchCode() {
  generating.value = true
  try {
    const res = await api<any>('/sdk/generate', {
      method: 'POST',
      body: JSON.stringify({ language: selectedLang.value, endpoints: selectedEndpoints.value })
    })
    generatedCode.value = res.code ?? generatedCode.value
    store.showToast('Code generated', 'ok')
  } catch {
    generateCode()
    store.showToast('Using local generator', 'info')
  } finally {
    generating.value = false
  }
}

function copyCode() {
  navigator.clipboard.writeText(generatedCode.value)
  store.showToast('Copied to clipboard', 'ok')
}

function downloadCode() {
  const ext: Record<string, string> = { python: 'py', typescript: 'ts', go: 'go', curl: 'sh' }
  const blob = new Blob([generatedCode.value], { type: 'text/plain' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `noc-client.${ext[selectedLang.value] ?? 'txt'}`
  a.click()
  store.showToast('Downloaded', 'ok')
}

function methodBadge(m: string) {
  if (m === 'GET') return 'badge-ok'
  return 'badge-info'
}

onMounted(generateCode)
</script>

<style scoped>
.sdk-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.sdk-layout {
  display: grid;
  grid-template-columns: 340px 1fr;
  gap: 1rem;
  align-items: start;
}

.config-panel { padding: 1rem; display: flex; flex-direction: column; gap: 1rem; }
.panel-title { font-weight: 700; font-size: .9rem; margin-bottom: .5rem; }

.field-group { display: flex; flex-direction: column; gap: .4rem; }
.field-label { font-size: .75rem; letter-spacing: .05em; }

.lang-buttons { flex-wrap: wrap; }

.endpoint-list {
  display: flex;
  flex-direction: column;
  gap: .3rem;
  max-height: 360px;
  overflow-y: auto;
  background: var(--bg);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  padding: .5rem;
}
.endpoint-item {
  display: flex;
  align-items: center;
  gap: .4rem;
  padding: .2rem .3rem;
  cursor: pointer;
  border-radius: var(--radius-sm);
  transition: var(--transition);
}
.endpoint-item:hover { background: var(--bg3); }
.ep-path { font-size: .75rem; flex: 1; }

.code-panel { padding: 1rem; }
.code-panel-header { margin-bottom: .75rem; }
.code-block {
  background: var(--bg);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  padding: 1rem;
  overflow: auto;
  max-height: 600px;
  font-family: var(--font-mono);
  font-size: .8rem;
  line-height: 1.6;
  color: var(--accent3);
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
