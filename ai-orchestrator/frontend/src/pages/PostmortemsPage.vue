<template>
  <div class="pm-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">📋 Postmortems</h1>
      <button class="btn btn-primary" @click="openNew">+ Novo Postmortem</button>
    </div>

    <div class="pm-layout">
      <!-- List -->
      <div class="card pm-list">
        <div class="card-title mb-2">Histórico ({{ items.length }})</div>
        <div
          v-for="pm in items"
          :key="pm.id"
          class="pm-row"
          :class="{ selected: selected?.id === pm.id }"
          @click="selected = pm"
        >
          <div class="pmr-head flex justify-between">
            <span class="pmr-title truncate">{{ pm.title }}</span>
            <span class="badge" :class="sevBadge(pm.severity)">{{ pm.severity }}</span>
          </div>
          <div class="pmr-meta flex gap-2 mt-1">
            <span class="text-muted" style="font-size:10px">{{ pm.date }}</span>
            <span class="badge" :class="pm.status === 'resolved' ? 'badge-ok' : 'badge-warn'">{{ pm.status }}</span>
          </div>
        </div>
      </div>

      <!-- Detail / Editor -->
      <div class="card pm-detail">
        <div v-if="!selected && !creating" class="no-selection">
          <div style="font-size:36px;opacity:0.2">📋</div>
          <div class="text-muted">Selecione um postmortem ou crie um novo</div>
        </div>

        <template v-else-if="editing || creating">
          <div class="pm-editor-header flex justify-between items-center mb-3">
            <h3>{{ creating ? 'Novo Postmortem' : 'Editar: ' + selected?.title }}</h3>
            <div class="flex gap-2">
              <button class="btn btn-ghost" @click="cancelEdit">Cancelar</button>
              <button class="btn btn-primary" @click="save">Salvar</button>
            </div>
          </div>
          <div class="form-grid">
            <label>
              <span class="form-label">Título</span>
              <input v-model="form.title" placeholder="Incidente: descrição breve" />
            </label>
            <div class="form-row">
              <label>
                <span class="form-label">Severidade</span>
                <select v-model="form.severity">
                  <option>P1</option><option>P2</option><option>P3</option><option>P4</option>
                </select>
              </label>
              <label>
                <span class="form-label">Status</span>
                <select v-model="form.status">
                  <option>open</option><option>in_review</option><option>resolved</option>
                </select>
              </label>
              <label>
                <span class="form-label">Data</span>
                <input v-model="form.date" type="date" />
              </label>
            </div>
            <label>
              <span class="form-label">Resumo do Impacto</span>
              <textarea v-model="form.summary" rows="3" placeholder="O que aconteceu e qual foi o impacto..."></textarea>
            </label>
            <label>
              <span class="form-label">Timeline</span>
              <textarea v-model="form.timeline" rows="5" placeholder="HH:MM — evento&#10;HH:MM — evento..."></textarea>
            </label>
            <label>
              <span class="form-label">Root Cause</span>
              <textarea v-model="form.root_cause" rows="3" placeholder="Causa raiz identificada..."></textarea>
            </label>
            <label>
              <span class="form-label">Action Items</span>
              <textarea v-model="form.action_items" rows="4" placeholder="- [ ] Ação 1&#10;- [ ] Ação 2..."></textarea>
            </label>
          </div>
        </template>

        <template v-else-if="selected">
          <div class="pm-view-header flex justify-between items-center mb-4">
            <div>
              <h2 style="font-size:16px;font-weight:700">{{ selected.title }}</h2>
              <div class="flex gap-2 mt-1">
                <span class="badge" :class="sevBadge(selected.severity)">{{ selected.severity }}</span>
                <span class="badge" :class="selected.status === 'resolved' ? 'badge-ok' : 'badge-warn'">{{ selected.status }}</span>
                <span class="text-muted" style="font-size:11px">{{ selected.date }}</span>
              </div>
            </div>
            <div class="flex gap-2">
              <button class="btn btn-ghost" @click="exportMd">⤓ MD</button>
              <button class="btn btn-ghost" @click="editing = true">✎ Editar</button>
            </div>
          </div>

          <div class="pm-sections">
            <div v-if="selected.summary" class="pm-section">
              <div class="ps-title">📌 Resumo</div>
              <p>{{ selected.summary }}</p>
            </div>
            <div v-if="selected.timeline" class="pm-section">
              <div class="ps-title">⏱ Timeline</div>
              <pre class="mono pm-pre">{{ selected.timeline }}</pre>
            </div>
            <div v-if="selected.root_cause" class="pm-section">
              <div class="ps-title">🔍 Root Cause</div>
              <p>{{ selected.root_cause }}</p>
            </div>
            <div v-if="selected.action_items" class="pm-section">
              <div class="ps-title">✅ Action Items</div>
              <pre class="mono pm-pre">{{ selected.action_items }}</pre>
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
import { useAppStore } from '@/stores/app'

const { api, apif } = useApi()
const app = useAppStore()

interface Postmortem {
  id: string
  title: string
  severity: string
  status: string
  date: string
  summary?: string
  timeline?: string
  root_cause?: string
  action_items?: string
}

const items = ref<Postmortem[]>([])
const selected = ref<Postmortem | null>(null)
const editing = ref(false)
const creating = ref(false)
const form = ref({ title: '', severity: 'P2', status: 'open', date: new Date().toISOString().substring(0, 10), summary: '', timeline: '', root_cause: '', action_items: '' })

function sevBadge(s: string) {
  return { 'badge-err': s === 'P1', 'badge-warn': s === 'P2', 'badge-info': s === 'P3' }
}

function openNew() {
  creating.value = true
  selected.value = null
  form.value = { title: '', severity: 'P2', status: 'open', date: new Date().toISOString().substring(0, 10), summary: '', timeline: '', root_cause: '', action_items: '' }
}

function cancelEdit() {
  editing.value = false
  creating.value = false
}

async function save() {
  try {
    const payload = creating.value ? form.value : { ...selected.value, ...form.value }
    await apif('/postmortems', payload)
    app.showToast('Postmortem salvo', 'ok')
    editing.value = false
    creating.value = false
    load()
  } catch { app.showToast('Erro ao salvar', 'err') }
}

function exportMd() {
  if (!selected.value) return
  const pm = selected.value
  const md = `# ${pm.title}\n\n**Severidade:** ${pm.severity} | **Status:** ${pm.status} | **Data:** ${pm.date}\n\n## Resumo\n${pm.summary ?? ''}\n\n## Timeline\n${pm.timeline ?? ''}\n\n## Root Cause\n${pm.root_cause ?? ''}\n\n## Action Items\n${pm.action_items ?? ''}\n`
  const blob = new Blob([md], { type: 'text/markdown' })
  const a = document.createElement('a')
  a.href = URL.createObjectURL(blob)
  a.download = `postmortem-${pm.id}.md`
  a.click()
  app.showToast('Exportado', 'ok')
}

async function load() {
  try {
    const d = await api<{ postmortems: Postmortem[] }>('/postmortems')
    items.value = d.postmortems ?? []
  } catch {
    items.value = [
      { id: '1', title: 'LLM Router timeout em cascata — 2026-03-15', severity: 'P1', status: 'resolved', date: '2026-03-15', summary: 'Timeout do provider OpenAI causou backlog de 340 tasks por 18 minutos.', timeline: '14:12 — Alertas P99 > 5s\n14:15 — On-call acionado\n14:23 — Circuit breaker ativado\n14:30 — Fallback para Claude ativado\n14:41 — Sistema normalizado', root_cause: 'Rate limit inesperado do provider sem retry adequado no LLM Router.', action_items: '- [x] Implementar retry exponencial\n- [ ] Adicionar fallback automático\n- [ ] SLO de fallback < 30s' },
      { id: '2', title: 'Redis OOM — perda de contexto de agentes', severity: 'P2', status: 'in_review', date: '2026-03-10', summary: 'Memória Redis esgotada, contextos de 12 agentes perdidos.', timeline: '09:30 — Redis alertou 95% memória\n09:45 — OOM, evictions iniciadas\n10:01 — Agentes sem contexto reportaram erro', root_cause: 'TTL não configurado em chaves de contexto de longa duração.', action_items: '- [x] TTL padrão de 24h para contextos\n- [ ] Política de eviction configurada\n- [ ] Alerta em 80% memória' },
    ]
  }
}

// Watch selected to load form for editing
function startEdit() {
  if (!selected.value) return
  Object.assign(form.value, selected.value)
}

onMounted(load)
</script>

<style scoped>
.pm-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mb-2 { margin-bottom: 8px; }
.mb-3 { margin-bottom: 12px; }
.mb-4 { margin-bottom: 16px; }
.mt-1 { margin-top: 4px; }

.pm-layout {
  display: grid;
  grid-template-columns: 280px 1fr;
  gap: 12px;
  align-items: start;
  min-height: 500px;
}

.pm-list { padding: 12px; max-height: calc(100vh - 200px); overflow-y: auto; }

.pm-row {
  padding: 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  border: 1px solid transparent;
  transition: var(--transition);
  margin-bottom: 4px;
}
.pm-row:hover { background: var(--bg3); }
.pm-row.selected { background: rgba(0,212,255,0.06); border-color: rgba(0,212,255,0.2); }
.pmr-title { font-size: 12.5px; font-weight: 500; max-width: 160px; }

.pm-detail { padding: 16px; min-height: 400px; }
.no-selection { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 300px; gap: 12px; }

/* Form */
.form-grid { display: flex; flex-direction: column; gap: 12px; }
.form-row { display: flex; gap: 12px; }
.form-row label { flex: 1; }
.form-label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }

/* View */
.pm-sections { display: flex; flex-direction: column; gap: 16px; }
.pm-section { }
.ps-title { font-size: 12px; font-weight: 700; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 6px; }
.pm-pre { background: var(--bg3); padding: 10px; border-radius: var(--radius-sm); font-size: 12px; line-height: 1.6; white-space: pre-wrap; }
p { font-size: 13px; line-height: 1.6; }

@media (max-width: 900px) {
  .pm-layout { grid-template-columns: 1fr; }
}
</style>
