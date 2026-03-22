<template>
  <div class="knowledge-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Knowledge Base Editor</h1>
        <p class="text-muted">View, edit and manage knowledge documents</p>
      </div>
      <button class="btn btn-ghost" @click="loadData" :disabled="loading">
        {{ loading ? '...' : 'Refresh' }}
      </button>
    </div>

    <div class="split-layout">
      <!-- Left: Document List -->
      <div class="card doc-list-panel">
        <div class="doc-list-header flex items-center justify-between">
          <span class="panel-section-title">Documents</span>
          <button class="btn btn-primary" style="font-size:.75rem; padding:.3rem .6rem;" @click="newDocument">+ New</button>
        </div>
        <input class="search-input" v-model="search" placeholder="Search documents..." style="width:100%; box-sizing:border-box;" />
        <div class="doc-items">
          <div
            v-for="doc in filteredDocs"
            :key="doc.id"
            class="doc-item"
            :class="{ selected: selectedDoc?.id === doc.id }"
            @click="selectDoc(doc)"
          >
            <div class="doc-item-title truncate">{{ doc.title }}</div>
            <div class="doc-item-meta flex items-center justify-between">
              <span class="badge badge-info">{{ doc.category }}</span>
              <div class="flex items-center gap-2">
                <div class="quality-dot" :class="qualityDotClass(doc.qualityScore)" :title="'Quality: ' + doc.qualityScore"></div>
                <span class="text-muted" style="font-size:.7rem;">{{ doc.qualityScore }}/100</span>
              </div>
            </div>
            <div class="text-muted" style="font-size:.7rem;">{{ doc.lastUpdated }}</div>
          </div>
          <div v-if="filteredDocs.length === 0" class="text-muted" style="padding:.75rem; font-size:.85rem; text-align:center;">
            No documents found
          </div>
        </div>
      </div>

      <!-- Right: Editor -->
      <div class="card editor-panel">
        <div v-if="!selectedDoc" class="empty-editor text-muted">
          Select a document to edit or create a new one
        </div>
        <template v-else>
          <div class="editor-header flex items-center justify-between" style="margin-bottom:.75rem;">
            <div style="flex:1; margin-right:1rem;">
              <input
                class="title-input"
                v-model="editTitle"
                placeholder="Document title"
              />
              <div class="flex gap-2 items-center" style="margin-top:.35rem;">
                <input class="category-input" v-model="editCategory" placeholder="Category" />
                <div class="quality-indicator">
                  <span class="text-muted" style="font-size:.7rem;">QUALITY</span>
                  <span class="quality-score" :class="qualityClass(selectedDoc.qualityScore)">
                    {{ selectedDoc.qualityScore }}/100
                  </span>
                </div>
              </div>
            </div>
            <div class="flex gap-2">
              <button class="btn btn-ghost" @click="saveDoc" :disabled="saving">
                {{ saving ? 'Saving...' : 'Save' }}
              </button>
              <button class="btn btn-danger" @click="deleteDoc">Delete</button>
            </div>
          </div>

          <textarea
            class="doc-editor"
            v-model="editContent"
            placeholder="Write document content here... Markdown supported."
          ></textarea>

          <div class="editor-footer flex items-center justify-between">
            <span class="text-muted" style="font-size:.75rem;">
              {{ editContent.length }} chars · {{ editContent.split(/\s+/).filter(Boolean).length }} words
            </span>
            <span class="text-muted" style="font-size:.75rem;">Last updated: {{ selectedDoc.lastUpdated }}</span>
          </div>
        </template>
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

interface KnowledgeDoc {
  id: string
  title: string
  category: string
  lastUpdated: string
  qualityScore: number
  content: string
}

const loading = ref(false)
const saving = ref(false)
const docs = ref<KnowledgeDoc[]>([])
const selectedDoc = ref<KnowledgeDoc | null>(null)
const search = ref('')
const editTitle = ref('')
const editCategory = ref('')
const editContent = ref('')

const demoDocs: KnowledgeDoc[] = [
  {
    id: 'doc-001', title: 'GDPR Data Retention Policy', category: 'Compliance',
    lastUpdated: '2026-03-20', qualityScore: 92,
    content: '# GDPR Data Retention Policy\n\nThis document outlines the data retention requirements under GDPR...\n\n## Key Principles\n\n- Data minimization: only collect what is necessary\n- Storage limitation: delete data after its purpose is fulfilled\n- Right to erasure: users can request deletion\n\n## Retention Periods\n\n| Data Type | Retention Period |\n|-----------|------------------|\n| User sessions | 90 days |\n| Logs | 1 year |\n| Audit trails | 7 years |'
  },
  {
    id: 'doc-002', title: 'RAG Pipeline Architecture', category: 'Technical',
    lastUpdated: '2026-03-19', qualityScore: 88,
    content: '# RAG Pipeline Architecture\n\n## Overview\n\nThe RAG (Retrieval-Augmented Generation) pipeline consists of...\n\n1. **Query Processing** — tokenize and embed the user query\n2. **Retrieval** — search Qdrant vector store with top-k=5\n3. **Reranking** — cross-encoder reranking for relevance\n4. **Context injection** — inject retrieved docs into LLM prompt\n5. **Generation** — LLM generates grounded response'
  },
  {
    id: 'doc-003', title: 'Incident Response Runbook', category: 'Operations',
    lastUpdated: '2026-03-18', qualityScore: 74,
    content: '# Incident Response Runbook\n\n## Severity Levels\n\n- **P0**: Complete outage — page on-call immediately\n- **P1**: Major degradation — response within 15 min\n- **P2**: Minor issues — response within 1 hour\n\n## First Steps\n\n1. Acknowledge the alert\n2. Check Grafana dashboard for root cause\n3. Check recent deployments\n4. Rollback if deployment-related'
  },
  {
    id: 'doc-004', title: 'LLM Provider Failover Policy', category: 'Technical',
    lastUpdated: '2026-03-15', qualityScore: 81,
    content: '# LLM Provider Failover Policy\n\nWhen a primary LLM provider fails circuit breaker threshold...\n\n## Failover Order\n\n1. OpenAI (primary)\n2. Anthropic (secondary)\n3. Groq (tertiary — lower cost, faster)\n4. Fallback to cached responses\n\n## Circuit Breaker Settings\n\n- Threshold: 5 errors in 60 seconds\n- Half-open probe: every 30 seconds'
  },
]

const filteredDocs = computed(() => {
  if (!search.value) return docs.value
  return docs.value.filter(d =>
    d.title.toLowerCase().includes(search.value.toLowerCase()) ||
    d.category.toLowerCase().includes(search.value.toLowerCase())
  )
})

function selectDoc(doc: KnowledgeDoc) {
  selectedDoc.value = doc
  editTitle.value = doc.title
  editCategory.value = doc.category
  editContent.value = doc.content
}

function newDocument() {
  const newDoc: KnowledgeDoc = {
    id: 'doc-new-' + Date.now(),
    title: 'New Document',
    category: 'General',
    lastUpdated: 'just now',
    qualityScore: 0,
    content: ''
  }
  docs.value.unshift(newDoc)
  selectDoc(newDoc)
}

async function saveDoc() {
  if (!selectedDoc.value) return
  saving.value = true
  try {
    await api('/knowledge/save', {
      method: 'POST',
      body: JSON.stringify({
        id: selectedDoc.value.id,
        title: editTitle.value,
        category: editCategory.value,
        content: editContent.value
      })
    })
    const idx = docs.value.findIndex(d => d.id === selectedDoc.value!.id)
    if (idx >= 0) {
      docs.value[idx] = { ...docs.value[idx], title: editTitle.value, category: editCategory.value, content: editContent.value, lastUpdated: 'just now' }
      selectedDoc.value = docs.value[idx]
    }
    store.showToast('Document saved', 'ok')
  } catch {
    store.showToast('Save failed', 'err')
  } finally {
    saving.value = false
  }
}

async function deleteDoc() {
  if (!selectedDoc.value) return
  if (!confirm(`Delete "${selectedDoc.value.title}"?`)) return
  try {
    await api('/knowledge/delete', { method: 'POST', body: JSON.stringify({ id: selectedDoc.value.id }) })
  } catch {}
  docs.value = docs.value.filter(d => d.id !== selectedDoc.value!.id)
  selectedDoc.value = null
  store.showToast('Document deleted', 'ok')
}

function qualityDotClass(score: number) {
  if (score >= 80) return 'qdot-ok'
  if (score >= 60) return 'qdot-warn'
  return 'qdot-err'
}
function qualityClass(score: number) {
  if (score >= 80) return 'text-ok'
  if (score >= 60) return 'text-warn'
  return 'text-danger'
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/knowledge/list')
    docs.value = res.docs ?? res
  } catch {
    docs.value = demoDocs
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.knowledge-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.split-layout {
  display: grid;
  grid-template-columns: 300px 1fr;
  gap: 1rem;
  align-items: start;
}

.doc-list-panel { padding: 1rem; display: flex; flex-direction: column; gap: .75rem; }
.doc-list-header { padding-bottom: .5rem; border-bottom: 1px solid var(--panel-border); }
.panel-section-title { font-weight: 700; font-size: .9rem; }

.search-input {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  padding: .35rem .6rem;
  font-size: .84rem;
  outline: none;
  transition: var(--transition);
}
.search-input:focus { border-color: var(--accent); }

.doc-items { display: flex; flex-direction: column; gap: .35rem; max-height: 600px; overflow-y: auto; }
.doc-item {
  padding: .6rem .75rem;
  border-radius: var(--radius-sm);
  border: 1px solid transparent;
  cursor: pointer;
  transition: var(--transition);
}
.doc-item:hover { background: var(--bg3); }
.doc-item.selected { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 8%, transparent); }
.doc-item-title { font-size: .88rem; font-weight: 600; margin-bottom: .3rem; }
.doc-item-meta { margin-bottom: .2rem; }

.quality-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
.qdot-ok { background: var(--accent3); }
.qdot-warn { background: var(--warn); }
.qdot-err { background: var(--danger); }

.editor-panel { padding: 1rem; display: flex; flex-direction: column; }
.empty-editor { padding: 3rem; text-align: center; font-size: .9rem; }

.title-input {
  width: 100%;
  background: none;
  border: none;
  border-bottom: 1px solid var(--panel-border);
  color: var(--text);
  font-size: 1.2rem;
  font-weight: 700;
  padding: .2rem 0;
  outline: none;
  box-sizing: border-box;
}
.title-input:focus { border-bottom-color: var(--accent); }

.category-input {
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  padding: .25rem .5rem;
  font-size: .8rem;
  outline: none;
  width: 140px;
}

.quality-indicator { display: flex; align-items: center; gap: .35rem; }
.quality-score { font-family: var(--font-mono); font-size: .85rem; font-weight: 700; }

.doc-editor {
  flex: 1;
  min-height: 400px;
  background: var(--bg);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  color: var(--text);
  font-family: var(--font-mono);
  font-size: .85rem;
  line-height: 1.7;
  padding: .75rem;
  resize: vertical;
  outline: none;
  box-sizing: border-box;
}
.doc-editor:focus { border-color: var(--accent); }

.editor-footer { margin-top: .5rem; padding-top: .5rem; border-top: 1px solid var(--panel-border); }
</style>
