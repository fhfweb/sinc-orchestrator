<template>
  <div class="tracing-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">◎ Distributed Tracing</h1>
      <div class="flex gap-2">
        <input v-model="searchQ" placeholder="Buscar trace ID / serviço..." style="width:240px" @input="load" />
        <select v-model="window_" @change="load">
          <option value="15m">15 min</option>
          <option value="1h">1 hora</option>
          <option value="6h">6 horas</option>
          <option value="24h">24 horas</option>
        </select>
        <button class="btn btn-ghost" @click="load">↻</button>
      </div>
    </div>

    <div class="tracing-layout">
      <!-- Trace list -->
      <div class="card trace-list">
        <div class="card-title mb-2">Traces ({{ traces.length }})</div>
        <div
          v-for="t in traces"
          :key="t.trace_id"
          class="trace-row"
          :class="{ selected: selected?.trace_id === t.trace_id, error: t.error }"
          @click="selected = t"
        >
          <div class="tr-top flex justify-between">
            <span class="mono" style="font-size:10px">{{ t.trace_id.substring(0,16) }}…</span>
            <span class="badge" :class="t.error ? 'badge-err' : 'badge-ok'">{{ t.error ? 'ERR' : 'OK' }}</span>
          </div>
          <div class="tr-svc text-muted" style="font-size:11px">{{ t.service }}</div>
          <div class="tr-meta flex justify-between mt-1">
            <span class="text-muted" style="font-size:10px">{{ t.operation }}</span>
            <span class="mono" style="font-size:10px" :class="t.duration_ms > 500 ? 'text-warn' : 'text-muted'">{{ t.duration_ms }}ms</span>
          </div>
        </div>
        <div v-if="traces.length === 0" class="text-muted" style="padding:20px;text-align:center">Sem traces no período</div>
      </div>

      <!-- Trace detail -->
      <div class="card trace-detail">
        <div v-if="!selected" class="no-selection">
          <div style="font-size:32px;opacity:0.2">◎</div>
          <div class="text-muted">Selecione um trace para ver detalhes</div>
        </div>
        <template v-else>
          <div class="td-header flex justify-between items-center">
            <div>
              <div class="mono" style="font-size:11px;color:var(--text-muted)">{{ selected.trace_id }}</div>
              <div style="font-size:14px;font-weight:600;margin-top:2px">{{ selected.operation }}</div>
            </div>
            <div class="flex gap-2">
              <span class="badge" :class="selected.error ? 'badge-err' : 'badge-ok'">
                {{ selected.error ? 'ERROR' : 'OK' }}
              </span>
              <span class="badge badge-info mono">{{ selected.duration_ms }}ms</span>
            </div>
          </div>

          <!-- Gantt-style spans -->
          <div class="spans-container mt-4" v-if="selected.spans?.length">
            <div class="span-header flex" style="margin-bottom:4px;font-size:10px;color:var(--text-muted)">
              <div style="width:200px">Serviço / Operação</div>
              <div style="flex:1">Timeline</div>
              <div style="width:60px;text-align:right">Dur.</div>
            </div>
            <div v-for="span in selected.spans" :key="span.span_id" class="span-row flex items-center">
              <div class="span-name" :style="`padding-left:${(span.depth ?? 0) * 14}px`">
                <span class="text-muted" style="font-size:9px">{{ span.service }}</span>
                <div style="font-size:11px">{{ span.operation }}</div>
              </div>
              <div class="span-bar-wrap">
                <div
                  class="span-bar"
                  :class="span.error ? 'span-err' : ''"
                  :style="`left:${pct(span.start_offset, selected.duration_ms)}%;width:${Math.max(pct(span.duration_ms, selected.duration_ms), 1)}%`"
                ></div>
              </div>
              <div class="span-dur mono">{{ span.duration_ms }}ms</div>
            </div>
          </div>

          <!-- Tags -->
          <div class="td-tags mt-4" v-if="selected.tags">
            <div class="card-title mb-2">Tags</div>
            <div class="tags-grid">
              <div v-for="(v, k) in selected.tags" :key="k" class="tag-row">
                <span class="tag-k text-muted mono">{{ k }}</span>
                <span class="tag-v mono">{{ v }}</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'

const { api } = useApi()

interface Span {
  span_id: string
  service: string
  operation: string
  start_offset: number
  duration_ms: number
  depth?: number
  error?: boolean
}

interface Trace {
  trace_id: string
  service: string
  operation: string
  duration_ms: number
  error?: boolean
  spans?: Span[]
  tags?: Record<string, string>
}

const traces = ref<Trace[]>([])
const selected = ref<Trace | null>(null)
const searchQ = ref('')
const window_ = ref('1h')

function pct(val: number, total: number) {
  return total > 0 ? Math.min((val / total) * 100, 100) : 0
}

const DEMO_TRACES: Trace[] = [
  {
    trace_id: 'abc123def456789012345678',
    service: 'orchestrator',
    operation: 'task.dispatch',
    duration_ms: 342,
    error: false,
    tags: { 'task.type': 'rag_query', 'tenant.id': 'default', 'agent.id': 'orch-0001' },
    spans: [
      { span_id: 's1', service: 'orchestrator', operation: 'task.dispatch', start_offset: 0, duration_ms: 342, depth: 0 },
      { span_id: 's2', service: 'rag-engine', operation: 'vector.search', start_offset: 12, duration_ms: 200, depth: 1 },
      { span_id: 's3', service: 'qdrant', operation: 'query', start_offset: 18, duration_ms: 160, depth: 2 },
      { span_id: 's4', service: 'llm-router', operation: 'completion', start_offset: 215, duration_ms: 120, depth: 1 },
    ]
  },
  {
    trace_id: 'err999aaa000bbb111ccc222',
    service: 'llm-router',
    operation: 'completion',
    duration_ms: 5002,
    error: true,
    tags: { 'error.type': 'timeout', 'model': 'gpt-4o', 'tenant.id': 'acme' },
    spans: [
      { span_id: 'e1', service: 'llm-router', operation: 'completion', start_offset: 0, duration_ms: 5002, depth: 0, error: true },
      { span_id: 'e2', service: 'openai', operation: 'http.post', start_offset: 5, duration_ms: 5000, depth: 1, error: true },
    ]
  },
  {
    trace_id: 'mem777xxx888yyy999zzz000',
    service: 'memory-sync',
    operation: 'memory.inject',
    duration_ms: 88,
    error: false,
    tags: { 'memory.type': 'episodic', 'count': '3' },
    spans: [
      { span_id: 'm1', service: 'memory-sync', operation: 'memory.inject', start_offset: 0, duration_ms: 88, depth: 0 },
      { span_id: 'm2', service: 'qdrant', operation: 'upsert', start_offset: 10, duration_ms: 65, depth: 1 },
    ]
  }
]

async function load() {
  try {
    const d = await api<{ traces: Trace[] }>('/tracing/traces', {
      params: { window: window_.value, search: searchQ.value || undefined }
    })
    traces.value = d.traces ?? []
  } catch {
    traces.value = DEMO_TRACES.filter(t =>
      !searchQ.value ||
      t.trace_id.includes(searchQ.value) ||
      t.service.includes(searchQ.value) ||
      t.operation.includes(searchQ.value)
    )
  }
}

onMounted(load)
</script>

<style scoped>
.tracing-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mb-2 { margin-bottom: 8px; }

.tracing-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 12px;
  align-items: start;
}

/* Trace list */
.trace-list { padding: 12px; max-height: calc(100vh - 180px); overflow-y: auto; }

.trace-row {
  padding: 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  border: 1px solid transparent;
  transition: var(--transition);
  margin-bottom: 4px;
}
.trace-row:hover { background: var(--bg3); }
.trace-row.selected { background: rgba(0,212,255,0.06); border-color: rgba(0,212,255,0.2); }
.trace-row.error { border-left: 3px solid var(--danger); }

/* Detail */
.trace-detail { padding: 16px; min-height: 400px; }

.no-selection {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 300px;
  gap: 12px;
}

.td-header { padding-bottom: 12px; border-bottom: 1px solid var(--panel-border); }

/* Spans */
.spans-container { border-top: 1px solid var(--panel-border); padding-top: 12px; }

.span-row {
  display: flex;
  align-items: center;
  height: 32px;
  gap: 8px;
  border-bottom: 1px solid rgba(255,255,255,0.03);
}
.span-name { width: 200px; flex-shrink: 0; overflow: hidden; }
.span-bar-wrap { flex: 1; height: 16px; position: relative; background: var(--bg3); border-radius: 2px; overflow: hidden; }
.span-bar {
  position: absolute;
  height: 100%;
  background: var(--accent);
  opacity: 0.7;
  border-radius: 2px;
  transition: width 0.3s;
}
.span-bar.span-err { background: var(--danger); }
.span-dur { width: 60px; text-align: right; font-size: 10px; color: var(--text-muted); }

/* Tags */
.tags-grid { display: flex; flex-direction: column; gap: 4px; }
.tag-row { display: flex; gap: 12px; font-size: 11.5px; }
.tag-k { width: 160px; flex-shrink: 0; }
.tag-v { color: var(--accent); }

@media (max-width: 900px) {
  .tracing-layout { grid-template-columns: 1fr; }
}
</style>
