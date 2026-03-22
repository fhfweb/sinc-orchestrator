<template>
  <div class="redis-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">⊡ Redis Browser</h1>
      <div class="flex gap-2">
        <input v-model="search" placeholder="Padrão de chave (ex: agent:*)" style="width:220px" @keydown.enter="loadKeys" />
        <button class="btn btn-primary" @click="loadKeys">Buscar</button>
        <button class="btn btn-ghost" @click="loadInfo">↻ Info</button>
      </div>
    </div>

    <!-- Stats -->
    <div class="redis-stats">
      <div class="card rs-card" v-for="s in stats" :key="s.label">
        <div class="rs-val mono" :class="s.cls">{{ s.val }}</div>
        <div class="rs-label text-muted">{{ s.label }}</div>
      </div>
    </div>

    <div class="redis-layout">
      <!-- Key list -->
      <div class="card key-list">
        <div class="kl-head flex justify-between items-center mb-2">
          <span class="card-title">Chaves ({{ keys.length }})</span>
          <button class="btn btn-danger" style="font-size:11px" @click="flushPattern" :disabled="!search">⊘ Flush padrão</button>
        </div>
        <div class="key-items">
          <div v-for="k in keys" :key="k.key" class="key-item" :class="{ selected: selected?.key === k.key }" @click="viewKey(k)">
            <div class="ki-key mono truncate">{{ k.key }}</div>
            <div class="ki-meta flex gap-2">
              <span class="badge">{{ k.type }}</span>
              <span class="text-muted" style="font-size:10px">TTL: {{ k.ttl === -1 ? '∞' : k.ttl + 's' }}</span>
            </div>
          </div>
        </div>
        <div v-if="keys.length === 0" class="text-muted" style="padding:20px;text-align:center">Nenhuma chave encontrada</div>
      </div>

      <!-- Key detail -->
      <div class="card key-detail">
        <div v-if="!selected" class="no-selection">
          <div style="font-size:32px;opacity:0.2">⊡</div>
          <div class="text-muted">Selecione uma chave</div>
        </div>
        <template v-else>
          <div class="kd-header flex justify-between items-center mb-3">
            <div class="mono" style="font-size:13px;font-weight:600">{{ selected.key }}</div>
            <div class="flex gap-2">
              <span class="badge badge-info">{{ selected.type }}</span>
              <span class="badge" :class="selected.ttl === -1 ? '' : 'badge-warn'">TTL: {{ selected.ttl === -1 ? '∞' : selected.ttl + 's' }}</span>
            </div>
          </div>
          <pre class="key-value mono">{{ typeof selected.value === 'object' ? JSON.stringify(selected.value, null, 2) : selected.value }}</pre>
        </template>
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

interface RedisKey { key: string; type: string; ttl: number; value?: unknown }

const keys = ref<RedisKey[]>([])
const selected = ref<RedisKey | null>(null)
const search = ref('*')

const stats = ref([
  { label: 'Memória usada', val: '—', cls: '' },
  { label: 'Total chaves', val: '—', cls: 'text-accent' },
  { label: 'Hits/s', val: '—', cls: 'text-ok' },
  { label: 'Misses/s', val: '—', cls: 'text-warn' },
  { label: 'Clientes', val: '—', cls: '' },
])

async function loadInfo() {
  try {
    const d = await api<Record<string, string>>('/redis/info')
    stats.value[0].val = d.used_memory_human ?? '—'
    stats.value[1].val = d.db_keys ?? '—'
    stats.value[2].val = d.keyspace_hits ?? '—'
    stats.value[3].val = d.keyspace_misses ?? '—'
    stats.value[4].val = d.connected_clients ?? '—'
  } catch {
    stats.value[0].val = '48.2MB'; stats.value[1].val = '1,842'; stats.value[2].val = '12.4k'; stats.value[3].val = '340'; stats.value[4].val = '8'
  }
}

async function loadKeys() {
  try {
    const d = await api<{ keys: RedisKey[] }>('/redis/keys', { params: { pattern: search.value } })
    keys.value = d.keys ?? []
  } catch {
    keys.value = [
      { key: 'agent:orch-0001:context', type: 'hash', ttl: 86400 },
      { key: 'agent:rag-0001:state', type: 'string', ttl: -1 },
      { key: 'tasks:queue:pending', type: 'list', ttl: -1 },
      { key: 'tenant:default:config', type: 'hash', ttl: -1 },
      { key: 'llm:cache:abc123', type: 'string', ttl: 3600 },
      { key: 'metrics:red:5m', type: 'hash', ttl: 300 },
    ].filter(k => !search.value || search.value === '*' || k.key.includes(search.value.replace('*', '')))
  }
}

async function viewKey(k: RedisKey) {
  selected.value = { ...k }
  try {
    const d = await api<{ value: unknown }>('/redis/get', { params: { key: k.key } })
    selected.value = { ...k, value: d.value }
  } catch {
    selected.value = { ...k, value: k.type === 'hash' ? { field1: 'value1', field2: 'value2', ts: Date.now() } : `demo-value-for-${k.key}` }
  }
}

async function flushPattern() {
  if (!confirm(`Flush de todas as chaves com padrão "${search.value}"?`)) return
  try {
    await apif('/redis/flush', { pattern: search.value })
    app.showToast('Flush executado', 'ok')
    loadKeys()
  } catch { app.showToast('Erro no flush', 'err') }
}

onMounted(() => { loadInfo(); loadKeys() })
</script>

<style scoped>
.redis-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mb-2 { margin-bottom: 8px; }
.mb-3 { margin-bottom: 12px; }

.redis-stats { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; }
.rs-card { padding: 12px; text-align: center; }
.rs-val { font-size: 20px; font-weight: 700; }
.rs-label { font-size: 11px; margin-top: 3px; }

.redis-layout { display: grid; grid-template-columns: 300px 1fr; gap: 12px; }

.key-list { padding: 12px; }
.key-items { max-height: calc(100vh - 300px); overflow-y: auto; display: flex; flex-direction: column; gap: 3px; }
.key-item { padding: 8px 10px; border-radius: var(--radius-sm); cursor: pointer; border: 1px solid transparent; transition: var(--transition); }
.key-item:hover { background: var(--bg3); }
.key-item.selected { background: rgba(0,212,255,0.06); border-color: rgba(0,212,255,0.2); }
.ki-key { font-size: 12px; margin-bottom: 4px; }
.ki-meta { margin-top: 3px; }

.key-detail { padding: 16px; min-height: 400px; }
.no-selection { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 300px; gap: 12px; }
.kd-header { padding-bottom: 12px; border-bottom: 1px solid var(--panel-border); }
.key-value { background: var(--bg3); padding: 12px; border-radius: var(--radius-sm); font-size: 12px; line-height: 1.6; overflow-x: auto; max-height: 500px; overflow-y: auto; white-space: pre-wrap; }

@media (max-width: 900px) { .redis-layout { grid-template-columns: 1fr; } .redis-stats { grid-template-columns: repeat(3, 1fr); } }
</style>
