<template>
  <div class="logs-page">
    <div class="logs-toolbar card">
      <div class="flex items-center gap-2">
        <button class="btn" :class="streaming ? 'btn-danger' : 'btn-primary'" @click="toggleStream">
          {{ streaming ? '⏹ Stop' : '▶ Live' }}
        </button>
        <select v-model="levelFilter" @change="applyFilter">
          <option value="">Todos níveis</option>
          <option>DEBUG</option><option>INFO</option><option>WARNING</option><option>ERROR</option>
        </select>
        <input v-model="textFilter" placeholder="Filtrar texto..." @input="applyFilter" style="width:220px" />
        <button class="btn btn-ghost" @click="clearLogs">⊘ Limpar</button>
      </div>
      <span class="text-muted" style="font-size:11px">{{ filtered.length }} linhas</span>
    </div>

    <div class="logs-container card mono" ref="containerRef">
      <div
        v-for="(line, i) in filtered"
        :key="i"
        class="log-line"
        :class="`level-${line.level?.toLowerCase()}`"
      >
        <span class="log-ts">{{ line.ts }}</span>
        <span class="log-level">{{ line.level }}</span>
        <span class="log-msg">{{ line.msg }}</span>
      </div>
      <div v-if="filtered.length === 0" class="logs-empty">Aguardando logs...</div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onUnmounted, nextTick } from 'vue'
import { useAppStore } from '@/stores/app'

interface LogLine { ts: string; level: string; msg: string }

const app = useAppStore()
const logs = ref<LogLine[]>([])
const streaming = ref(false)
const levelFilter = ref('')
const textFilter = ref('')
const containerRef = ref<HTMLElement>()
let ws: WebSocket | null = null

const filtered = computed(() => {
  return logs.value.filter(l => {
    if (levelFilter.value && l.level !== levelFilter.value) return false
    if (textFilter.value && !l.msg.toLowerCase().includes(textFilter.value.toLowerCase())) return false
    return true
  })
})

function scrollBottom() {
  nextTick(() => {
    if (containerRef.value) containerRef.value.scrollTop = containerRef.value.scrollHeight
  })
}

function toggleStream() {
  if (streaming.value) {
    ws?.close()
    ws = null
    streaming.value = false
    return
  }
  const tid = app.tenantId
  const wsUrl = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws/logs?tenant_id=${tid}`
  try {
    ws = new WebSocket(wsUrl)
    ws.onopen = () => { streaming.value = true }
    ws.onmessage = (e) => {
      try {
        const d = JSON.parse(e.data)
        logs.value.push({ ts: d.timestamp ?? new Date().toISOString().substring(11,19), level: d.level ?? 'INFO', msg: d.message ?? e.data })
        if (logs.value.length > 2000) logs.value.splice(0, 500)
        scrollBottom()
      } catch {
        logs.value.push({ ts: new Date().toISOString().substring(11,19), level: 'INFO', msg: e.data })
      }
    }
    ws.onerror = () => { streaming.value = false }
    ws.onclose = () => { streaming.value = false }
  } catch { /* no ws */ }
}

function clearLogs() { logs.value = [] }
function applyFilter() { /* computed handles it */ }

onUnmounted(() => ws?.close())
</script>

<style scoped>
.logs-page { display: flex; flex-direction: column; gap: 12px; height: calc(100vh - var(--topbar-h) - 40px); }

.logs-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px;
  flex-shrink: 0;
}

.logs-container {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  background: #050508;
  font-size: 11.5px;
  line-height: 1.6;
}

.log-line { display: flex; gap: 10px; padding: 1px 0; }
.log-ts { color: var(--text-dim); flex-shrink: 0; }
.log-level { width: 56px; flex-shrink: 0; font-weight: 600; }
.log-msg { word-break: break-all; }

.level-debug .log-level { color: var(--text-muted); }
.level-info .log-level { color: var(--accent); }
.level-warning .log-level { color: var(--warn); }
.level-error .log-level { color: var(--danger); }
.level-error .log-msg { color: #fca5a5; }

.logs-empty { color: var(--text-dim); padding: 20px; text-align: center; }
</style>
