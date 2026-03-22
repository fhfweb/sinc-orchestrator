<template>
  <div class="db-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">⊞ DB Console</h1>
      <div class="flex gap-2">
        <span class="badge badge-warn">read-only</span>
        <button class="btn btn-ghost" @click="loadSchema">↻ Schema</button>
      </div>
    </div>

    <div class="db-layout">
      <!-- Schema tree -->
      <div class="card schema-panel">
        <div class="card-title mb-2">Schema</div>
        <div v-for="t in tables" :key="t.name" class="table-item">
          <div class="ti-name" @click="quickQuery(t.name)">
            <span class="text-accent">▤</span> {{ t.name }}
            <span class="text-muted" style="font-size:10px">{{ t.rows }}</span>
          </div>
          <div class="ti-cols" v-if="t.expanded">
            <div v-for="c in t.columns" :key="c.name" class="col-item mono">
              <span class="text-muted">{{ c.name }}</span>
              <span class="badge" style="font-size:9px">{{ c.type }}</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Query panel -->
      <div class="card query-panel">
        <div class="qp-top">
          <div class="qp-toolbar flex justify-between items-center mb-2">
            <span class="card-title">Query Editor</span>
            <div class="flex gap-2">
              <button class="btn btn-ghost" style="font-size:11px" @click="sql = ''">limpar</button>
              <button class="btn btn-primary" :disabled="running" @click="runQuery">
                {{ running ? '…' : '▶ Executar' }}
              </button>
            </div>
          </div>
          <textarea v-model="sql" class="sql-editor mono" rows="5" placeholder="SELECT * FROM tasks LIMIT 20;" spellcheck="false"></textarea>
          <div class="quick-queries flex gap-2 mt-2">
            <button v-for="q in quickQueries" :key="q.label" class="btn btn-ghost" style="font-size:11px" @click="sql = q.sql">{{ q.label }}</button>
          </div>
        </div>

        <!-- Results -->
        <div class="qp-results mt-3">
          <div v-if="error" class="result-error">{{ error }}</div>
          <div v-else-if="result">
            <div class="result-meta flex justify-between items-center mb-2">
              <span class="text-muted" style="font-size:11px">{{ result.rows.length }} linhas • {{ result.elapsed_ms }}ms</span>
              <button class="btn btn-ghost" style="font-size:11px" @click="exportCsv">⤓ CSV</button>
            </div>
            <div class="result-table-wrap">
              <table class="tbl result-tbl">
                <thead>
                  <tr><th v-for="col in result.columns" :key="col">{{ col }}</th></tr>
                </thead>
                <tbody>
                  <tr v-for="(row, i) in result.rows" :key="i">
                    <td v-for="col in result.columns" :key="col" class="mono">{{ row[col] }}</td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
          <div v-else class="text-muted" style="padding:20px;text-align:center">Resultado aparecerá aqui</div>
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

interface TableInfo { name: string; rows: string; expanded: boolean; columns: { name: string; type: string }[] }
interface QueryResult { columns: string[]; rows: Record<string, unknown>[]; elapsed_ms: number }

const tables = ref<TableInfo[]>([])
const sql = ref('SELECT * FROM tasks ORDER BY created_at DESC LIMIT 20;')
const result = ref<QueryResult | null>(null)
const error = ref('')
const running = ref(false)

const quickQueries = [
  { label: 'Tasks recentes', sql: 'SELECT id, status, type, created_at FROM tasks ORDER BY created_at DESC LIMIT 20;' },
  { label: 'Agentes ativos', sql: "SELECT id, name, status, last_heartbeat FROM agents WHERE status != 'idle' LIMIT 20;" },
  { label: 'Erros 24h', sql: "SELECT COUNT(*) as total, type FROM tasks WHERE status='failed' AND created_at > NOW() - INTERVAL '24 hours' GROUP BY type;" },
  { label: 'Top tenants', sql: 'SELECT tenant_id, COUNT(*) as tasks FROM tasks GROUP BY tenant_id ORDER BY tasks DESC LIMIT 10;' },
]

async function runQuery() {
  error.value = ''
  result.value = null
  running.value = true
  try {
    const d = await apif<QueryResult>('/db/query', { sql: sql.value })
    result.value = d
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : 'Erro ao executar query'
  } finally {
    running.value = false
  }
}

async function quickQuery(tableName: string) {
  sql.value = `SELECT * FROM ${tableName} LIMIT 20;`
  runQuery()
}

async function loadSchema() {
  try {
    const d = await api<{ tables: TableInfo[] }>('/db/schema')
    tables.value = d.tables ?? []
  } catch {
    tables.value = [
      { name: 'tasks', rows: '~12k', expanded: false, columns: [{ name: 'id', type: 'uuid' }, { name: 'status', type: 'text' }, { name: 'type', type: 'text' }, { name: 'created_at', type: 'timestamp' }] },
      { name: 'agents', rows: '~24', expanded: false, columns: [{ name: 'id', type: 'text' }, { name: 'name', type: 'text' }, { name: 'status', type: 'text' }] },
      { name: 'projects', rows: '~8', expanded: false, columns: [{ name: 'id', type: 'uuid' }, { name: 'name', type: 'text' }] },
      { name: 'audit_log', rows: '~340k', expanded: false, columns: [{ name: 'id', type: 'uuid' }, { name: 'action', type: 'text' }, { name: 'ts', type: 'timestamp' }] },
    ]
  }
}

function exportCsv() {
  if (!result.value) return
  const rows = [result.value.columns.join(','), ...result.value.rows.map(r => result.value!.columns.map(c => String(r[c] ?? '')).join(','))]
  const blob = new Blob([rows.join('\n')], { type: 'text/csv' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = 'query_result.csv'
  a.click()
  app.showToast('CSV exportado', 'ok')
}

onMounted(loadSchema)
</script>

<style scoped>
.db-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mb-2 { margin-bottom: 8px; }
.mt-2 { margin-top: 8px; }
.mt-3 { margin-top: 12px; }

.db-layout { display: grid; grid-template-columns: 220px 1fr; gap: 12px; align-items: start; }

.schema-panel { padding: 12px; }
.table-item { margin-bottom: 4px; }
.ti-name { padding: 6px 8px; border-radius: var(--radius-sm); cursor: pointer; font-size: 12px; display: flex; justify-content: space-between; align-items: center; }
.ti-name:hover { background: var(--bg3); }
.col-item { display: flex; justify-content: space-between; align-items: center; padding: 3px 8px 3px 20px; font-size: 11px; }

.query-panel { padding: 16px; }
.sql-editor { width: 100%; background: var(--bg3); border-color: var(--panel-border); font-size: 12.5px; line-height: 1.5; border-radius: var(--radius-sm); color: var(--accent3); resize: vertical; }
.quick-queries { flex-wrap: wrap; }
.quick-queries .btn { font-size: 11px; padding: 4px 8px; }

.result-error { background: rgba(239,68,68,0.1); border: 1px solid var(--danger); border-radius: var(--radius-sm); padding: 10px; color: var(--danger); font-family: var(--font-mono); font-size: 12px; }
.result-table-wrap { overflow-x: auto; max-height: 400px; overflow-y: auto; }
.result-tbl td { font-size: 11.5px; white-space: nowrap; max-width: 240px; overflow: hidden; text-overflow: ellipsis; }

@media (max-width: 900px) { .db-layout { grid-template-columns: 1fr; } }
</style>
