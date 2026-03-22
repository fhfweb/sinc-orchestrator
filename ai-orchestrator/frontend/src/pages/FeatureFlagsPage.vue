<template>
  <div class="ff-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">⚑ Feature Flags</h1>
      <button class="btn btn-primary" @click="openModal()">+ Nova Flag</button>
    </div>

    <div class="ff-stats flex gap-3 mb-4">
      <div class="card stat-card">
        <div class="stat-val text-ok">{{ flags.filter(f=>f.enabled).length }}</div>
        <div class="stat-label text-muted">Ativas</div>
      </div>
      <div class="card stat-card">
        <div class="stat-val text-muted">{{ flags.filter(f=>!f.enabled).length }}</div>
        <div class="stat-label text-muted">Inativas</div>
      </div>
      <div class="card stat-card">
        <div class="stat-val text-warn">{{ flags.filter(f=>f.rollout_pct && f.rollout_pct < 100).length }}</div>
        <div class="stat-label text-muted">Rollout parcial</div>
      </div>
    </div>

    <div class="card">
      <div class="ff-toolbar flex gap-2 mb-3">
        <input v-model="search" placeholder="Buscar flag..." style="width:220px" />
        <select v-model="statusFilter">
          <option value="">Todas</option>
          <option value="enabled">Ativas</option>
          <option value="disabled">Inativas</option>
        </select>
      </div>

      <table class="tbl">
        <thead>
          <tr>
            <th>Flag</th>
            <th>Descrição</th>
            <th>Rollout</th>
            <th>Tenants</th>
            <th>Status</th>
            <th></th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="f in filtered" :key="f.key">
            <td class="mono" style="font-size:11.5px;font-weight:600">{{ f.key }}</td>
            <td class="text-muted" style="max-width:220px" >{{ f.description }}</td>
            <td>
              <div class="rollout-bar">
                <div class="rb-fill" :style="`width:${f.rollout_pct ?? 100}%`" :class="f.enabled ? 'rb-on' : 'rb-off'"></div>
                <span class="rb-label">{{ f.rollout_pct ?? 100 }}%</span>
              </div>
            </td>
            <td>
              <span v-if="!f.tenants?.length" class="text-muted">global</span>
              <span v-else class="badge">{{ f.tenants.length }} tenants</span>
            </td>
            <td>
              <label class="toggle">
                <input type="checkbox" :checked="f.enabled" @change="toggleFlag(f)" />
                <span class="toggle-track"></span>
              </label>
            </td>
            <td>
              <div class="flex gap-1">
                <button class="btn btn-ghost" style="font-size:11px;padding:3px 8px" @click="openModal(f)">✎</button>
                <button class="btn btn-danger" style="font-size:11px;padding:3px 8px" @click="deleteFlag(f.key)">✕</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <div v-if="filtered.length === 0" class="text-muted" style="padding:20px;text-align:center">Nenhuma flag encontrada</div>
    </div>

    <!-- Modal -->
    <div v-if="modalOpen" class="modal-overlay" @click.self="modalOpen = false">
      <div class="modal-box card">
        <h3 class="modal-title">{{ editFlag ? 'Editar Flag' : 'Nova Feature Flag' }}</h3>
        <div class="form-grid mt-4">
          <label>
            <span class="form-label">Key</span>
            <input v-model="form.key" placeholder="feature.nova_ui" :disabled="!!editFlag" />
          </label>
          <label>
            <span class="form-label">Descrição</span>
            <input v-model="form.description" placeholder="Descrição da feature" />
          </label>
          <label>
            <span class="form-label">Rollout %</span>
            <div class="flex items-center gap-2">
              <input v-model.number="form.rollout_pct" type="range" min="0" max="100" style="flex:1" />
              <span class="mono" style="width:36px">{{ form.rollout_pct }}%</span>
            </div>
          </label>
          <label class="perm-check">
            <input type="checkbox" v-model="form.enabled" />
            <span>Ativa por padrão</span>
          </label>
        </div>
        <div class="flex gap-2 justify-end mt-4">
          <button class="btn btn-ghost" @click="modalOpen = false">Cancelar</button>
          <button class="btn btn-primary" @click="saveFlag">Salvar</button>
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

interface Flag {
  key: string
  description?: string
  enabled: boolean
  rollout_pct?: number
  tenants?: string[]
}

const flags = ref<Flag[]>([])
const search = ref('')
const statusFilter = ref('')
const modalOpen = ref(false)
const editFlag = ref<Flag | null>(null)
const form = ref({ key: '', description: '', enabled: true, rollout_pct: 100 })

const filtered = computed(() =>
  flags.value.filter(f => {
    if (search.value && !f.key.includes(search.value) && !f.description?.includes(search.value)) return false
    if (statusFilter.value === 'enabled' && !f.enabled) return false
    if (statusFilter.value === 'disabled' && f.enabled) return false
    return true
  })
)

function openModal(flag?: Flag) {
  editFlag.value = flag ?? null
  form.value = { key: flag?.key ?? '', description: flag?.description ?? '', enabled: flag?.enabled ?? true, rollout_pct: flag?.rollout_pct ?? 100 }
  modalOpen.value = true
}

async function toggleFlag(f: Flag) {
  f.enabled = !f.enabled
  try {
    await apif('/feature-flags/' + f.key + '/toggle', { enabled: f.enabled })
    app.showToast(`Flag ${f.key} ${f.enabled ? 'ativada' : 'desativada'}`, f.enabled ? 'ok' : 'warn')
  } catch { app.showToast('Erro ao atualizar flag', 'err'); f.enabled = !f.enabled }
}

async function saveFlag() {
  try {
    await apif('/feature-flags', form.value)
    app.showToast('Flag salva', 'ok')
    modalOpen.value = false
    load()
  } catch { app.showToast('Erro ao salvar', 'err') }
}

async function deleteFlag(key: string) {
  if (!confirm(`Remover flag "${key}"?`)) return
  try {
    await api('/feature-flags/' + key, { method: 'DELETE' })
    app.showToast('Flag removida', 'ok')
    load()
  } catch { app.showToast('Erro ao remover', 'err') }
}

async function load() {
  try {
    const d = await api<{ flags: Flag[] }>('/feature-flags')
    flags.value = d.flags ?? []
  } catch {
    flags.value = [
      { key: 'feature.neo4j_graph', description: 'Visualização de grafo Neo4j no dashboard', enabled: true, rollout_pct: 100 },
      { key: 'feature.crt_terminal', description: 'Estilo CRT para o terminal MCTS', enabled: true, rollout_pct: 100 },
      { key: 'feature.mcts_v2', description: 'MCTS engine versão 2 com alpha-beta pruning', enabled: false, rollout_pct: 0 },
      { key: 'feature.rag_streaming', description: 'Respostas RAG via Server-Sent Events', enabled: true, rollout_pct: 50, tenants: ['acme', 'demo'] },
      { key: 'feature.chaos_auto', description: 'Experimentos de chaos automáticos agendados', enabled: false, rollout_pct: 0 },
    ]
  }
}

onMounted(load)
</script>

<style scoped>
.ff-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.mb-3 { margin-bottom: 12px; }
.mb-4 { margin-bottom: 16px; }
.mt-4 { margin-top: 16px; }

.ff-stats { flex-wrap: wrap; }
.stat-card { padding: 12px 20px; text-align: center; min-width: 100px; }
.stat-val { font-size: 28px; font-weight: 800; font-family: var(--font-mono); }
.stat-label { font-size: 11px; margin-top: 2px; }

/* Rollout bar */
.rollout-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 16px;
  background: var(--bg3);
  border-radius: 8px;
  overflow: hidden;
  position: relative;
  width: 100px;
}
.rb-fill {
  position: absolute;
  left: 0; top: 0; bottom: 0;
  border-radius: 8px;
  transition: width 0.3s;
}
.rb-on { background: var(--accent3); opacity: 0.6; }
.rb-off { background: var(--text-dim); }
.rb-label {
  position: absolute;
  right: 6px;
  font-size: 10px;
  color: var(--text);
  font-family: var(--font-mono);
}

/* Toggle switch */
.toggle { display: flex; align-items: center; cursor: pointer; }
.toggle input { display: none; }
.toggle-track {
  width: 36px; height: 20px;
  background: var(--text-dim);
  border-radius: 10px;
  position: relative;
  transition: var(--transition);
}
.toggle-track::after {
  content: '';
  position: absolute;
  left: 2px; top: 2px;
  width: 16px; height: 16px;
  background: white;
  border-radius: 50%;
  transition: var(--transition);
}
.toggle input:checked + .toggle-track { background: var(--accent3); }
.toggle input:checked + .toggle-track::after { left: 18px; }

/* Form */
.form-grid { display: flex; flex-direction: column; gap: 12px; }
.form-label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.perm-check { display: flex; align-items: center; gap: 8px; font-size: 13px; cursor: pointer; }
.perm-check input { width: auto; accent-color: var(--accent); }

/* Modal */
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 500; backdrop-filter: blur(4px); }
.modal-box { width: 440px; padding: 20px; }
.modal-title { font-size: 15px; font-weight: 700; }
</style>
