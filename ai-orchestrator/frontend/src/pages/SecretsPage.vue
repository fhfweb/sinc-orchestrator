<template>
  <div class="secrets-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">🔑 Secrets Manager</h1>
      <button class="btn btn-primary" @click="openModal()">+ Novo Secret</button>
    </div>

    <div class="card">
      <div class="secrets-toolbar flex gap-2 mb-3">
        <input v-model="search" placeholder="Buscar secret..." style="width:220px" />
        <select v-model="categoryFilter">
          <option value="">Todas categorias</option>
          <option>api_key</option><option>password</option><option>token</option><option>certificate</option>
        </select>
      </div>

      <table class="tbl">
        <thead>
          <tr><th>Nome</th><th>Categoria</th><th>Criado</th><th>Rotacionado</th><th>Usado por</th><th></th></tr>
        </thead>
        <tbody>
          <tr v-for="s in filtered" :key="s.name">
            <td>
              <div class="secret-name flex items-center gap-2">
                <span class="s-icon">🔑</span>
                <span class="mono">{{ s.name }}</span>
              </div>
            </td>
            <td><span class="badge badge-info">{{ s.category }}</span></td>
            <td class="text-muted" style="font-size:11px">{{ s.created_at }}</td>
            <td>
              <span class="text-muted" style="font-size:11px" :class="daysOld(s.rotated_at) > 90 ? 'text-danger' : ''">
                {{ s.rotated_at ?? 'nunca' }}
                <span v-if="daysOld(s.rotated_at) > 90" class="badge badge-err" style="font-size:9px">vencido</span>
              </span>
            </td>
            <td class="text-muted" style="font-size:11px">{{ s.used_by?.join(', ') ?? '—' }}</td>
            <td>
              <div class="flex gap-1">
                <button class="btn btn-ghost" style="font-size:11px;padding:3px 8px" @click="rotate(s.name)">↻ Rotar</button>
                <button class="btn btn-ghost" style="font-size:11px;padding:3px 8px" @click="openModal(s)">✎</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Modal -->
    <div v-if="modalOpen" class="modal-overlay" @click.self="modalOpen = false">
      <div class="modal-box card">
        <h3 class="modal-title">{{ editSecret ? 'Editar Secret' : 'Novo Secret' }}</h3>
        <div class="form-grid mt-3">
          <label><span class="form-label">Nome</span><input v-model="form.name" placeholder="OPENAI_API_KEY" :disabled="!!editSecret" /></label>
          <label><span class="form-label">Categoria</span>
            <select v-model="form.category">
              <option>api_key</option><option>password</option><option>token</option><option>certificate</option>
            </select>
          </label>
          <label><span class="form-label">Valor</span>
            <div class="secret-input flex gap-2">
              <input v-model="form.value" :type="showVal ? 'text' : 'password'" placeholder="sk-…" />
              <button class="btn btn-ghost" style="padding:6px 10px" @click="showVal = !showVal">{{ showVal ? '🙈' : '👁' }}</button>
            </div>
          </label>
        </div>
        <div class="flex gap-2 justify-end mt-4">
          <button class="btn btn-ghost" @click="modalOpen = false">Cancelar</button>
          <button class="btn btn-primary" @click="save">Salvar</button>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api, apif } = useApi()
const app = useAppStore()

interface Secret { name: string; category: string; created_at?: string; rotated_at?: string; used_by?: string[] }

const secrets = ref<Secret[]>([])
const search = ref('')
const categoryFilter = ref('')
const modalOpen = ref(false)
const editSecret = ref<Secret | null>(null)
const showVal = ref(false)
const form = ref({ name: '', category: 'api_key', value: '' })

const filtered = computed(() => secrets.value.filter(s =>
  (!search.value || s.name.toLowerCase().includes(search.value.toLowerCase())) &&
  (!categoryFilter.value || s.category === categoryFilter.value)
))

function daysOld(date?: string): number {
  if (!date) return 9999
  return Math.floor((Date.now() - new Date(date).getTime()) / 86400000)
}

function openModal(s?: Secret) {
  editSecret.value = s ?? null
  form.value = { name: s?.name ?? '', category: s?.category ?? 'api_key', value: '' }
  showVal.value = false
  modalOpen.value = true
}

async function save() {
  try {
    await apif('/secrets', form.value)
    app.showToast('Secret salvo', 'ok')
    modalOpen.value = false
    load()
  } catch { app.showToast('Erro ao salvar secret', 'err') }
}

async function rotate(name: string) {
  if (!confirm(`Rotar secret "${name}"? O valor atual será substituído.`)) return
  try {
    await apif('/secrets/rotate', { name })
    app.showToast('Secret rotacionado', 'ok')
    load()
  } catch { app.showToast('Erro ao rotar', 'err') }
}

async function load() {
  try {
    const d = await api<{ secrets: Secret[] }>('/secrets')
    secrets.value = d.secrets ?? []
  } catch {
    secrets.value = [
      { name: 'OPENAI_API_KEY', category: 'api_key', created_at: '2026-01-10', rotated_at: '2026-02-15', used_by: ['llm-router', 'playground'] },
      { name: 'ANTHROPIC_API_KEY', category: 'api_key', created_at: '2026-01-10', rotated_at: '2026-03-01', used_by: ['llm-router'] },
      { name: 'POSTGRES_PASSWORD', category: 'password', created_at: '2025-11-01', rotated_at: '2025-12-01', used_by: ['orchestrator'] },
      { name: 'QDRANT_API_TOKEN', category: 'token', created_at: '2026-01-15', rotated_at: undefined, used_by: ['rag-engine'] },
      { name: 'REDIS_PASSWORD', category: 'password', created_at: '2025-10-01', rotated_at: '2025-10-01', used_by: ['orchestrator', 'memory-sync'] },
    ]
  }
}

onMounted(load)
</script>

<style scoped>
.secrets-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.mb-3 { margin-bottom: 12px; }
.mt-3 { margin-top: 12px; }
.mt-4 { margin-top: 16px; }
.secret-name { font-size: 12.5px; }
.s-icon { font-size: 14px; }
.form-grid { display: flex; flex-direction: column; gap: 10px; }
.form-label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.secret-input { display: flex; gap: 6px; }
.secret-input input { flex: 1; }
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 500; backdrop-filter: blur(4px); }
.modal-box { width: 440px; padding: 20px; }
.modal-title { font-size: 15px; font-weight: 700; }
</style>
