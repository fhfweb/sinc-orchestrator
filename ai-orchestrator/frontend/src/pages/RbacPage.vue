<template>
  <div class="rbac-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">⊛ RBAC — Controle de Acesso</h1>
      <button class="btn btn-primary" @click="openModal()">+ Novo Role</button>
    </div>

    <div class="rbac-layout">
      <!-- Roles list -->
      <div class="card roles-panel">
        <div class="card-title mb-2">Roles ({{ roles.length }})</div>
        <div class="role-search mb-2">
          <input v-model="search" placeholder="Buscar role..." />
        </div>
        <div
          v-for="r in filteredRoles"
          :key="r.name"
          class="role-item"
          :class="{ selected: selected?.name === r.name }"
          @click="selected = r"
        >
          <div class="ri-name">{{ r.name }}</div>
          <div class="ri-meta flex gap-2">
            <span class="badge">{{ r.permissions?.length ?? 0 }} perms</span>
            <span class="badge" :class="r.system ? 'badge-info' : ''">{{ r.system ? 'system' : 'custom' }}</span>
          </div>
        </div>
      </div>

      <!-- Permissions matrix -->
      <div class="card perms-panel">
        <div v-if="!selected" class="no-selection">
          <div style="font-size:36px;opacity:0.2">⊛</div>
          <div class="text-muted">Selecione um role</div>
        </div>
        <template v-else>
          <div class="perms-header flex justify-between items-center mb-4">
            <div>
              <div class="role-title">{{ selected.name }}</div>
              <div class="text-muted" style="font-size:11px">{{ selected.description }}</div>
            </div>
            <div class="flex gap-2">
              <button class="btn btn-ghost" @click="openModal(selected)">Editar</button>
              <button class="btn btn-danger" :disabled="selected.system" @click="deleteRole(selected.name)">Remover</button>
            </div>
          </div>

          <div class="perms-grid">
            <div v-for="section in permSections" :key="section.label" class="perm-section">
              <div class="ps-label">{{ section.label }}</div>
              <div v-for="p in section.perms" :key="p" class="perm-row">
                <label class="perm-check">
                  <input
                    type="checkbox"
                    :checked="hasPerm(p)"
                    :disabled="selected.system"
                    @change="togglePerm(p)"
                  />
                  <span class="perm-name mono">{{ p }}</span>
                </label>
              </div>
            </div>
          </div>

          <div v-if="!selected.system" class="flex justify-end mt-4">
            <button class="btn btn-primary" @click="savePerms">Salvar Permissões</button>
          </div>
        </template>
      </div>
    </div>

    <!-- Modal -->
    <div v-if="modalOpen" class="modal-overlay" @click.self="modalOpen = false">
      <div class="modal-box card">
        <h3 class="modal-title">{{ editRole ? 'Editar Role' : 'Novo Role' }}</h3>
        <div class="flex flex-col gap-2 mt-4">
          <label class="text-muted" style="font-size:11px">Nome</label>
          <input v-model="form.name" placeholder="ex: analyst" :disabled="!!editRole" />
          <label class="text-muted" style="font-size:11px">Descrição</label>
          <input v-model="form.description" placeholder="Descrição do role" />
        </div>
        <div class="flex gap-2 justify-end mt-4">
          <button class="btn btn-ghost" @click="modalOpen = false">Cancelar</button>
          <button class="btn btn-primary" @click="saveRole">Salvar</button>
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

interface Role {
  name: string
  description?: string
  permissions?: string[]
  system?: boolean
}

const roles = ref<Role[]>([])
const selected = ref<Role | null>(null)
const search = ref('')
const modalOpen = ref(false)
const editRole = ref<Role | null>(null)
const form = ref({ name: '', description: '' })

const filteredRoles = computed(() =>
  roles.value.filter(r => !search.value || r.name.toLowerCase().includes(search.value.toLowerCase()))
)

const permSections = [
  {
    label: 'Tarefas',
    perms: ['tasks:read', 'tasks:write', 'tasks:cancel', 'tasks:delete']
  },
  {
    label: 'Agentes',
    perms: ['agents:read', 'agents:kill', 'agents:configure']
  },
  {
    label: 'Admin',
    perms: ['admin:read', 'admin:write', 'admin:rbac', 'admin:chaos']
  },
  {
    label: 'Dados',
    perms: ['data:read', 'data:write', 'data:export', 'secrets:read', 'secrets:write']
  },
  {
    label: 'Infra',
    perms: ['redis:read', 'redis:flush', 'db:query', 'migrations:run', 'chaos:run']
  }
]

function hasPerm(p: string) {
  return selected.value?.permissions?.includes(p) ?? false
}

function togglePerm(p: string) {
  if (!selected.value) return
  const perms = [...(selected.value.permissions ?? [])]
  const idx = perms.indexOf(p)
  if (idx >= 0) perms.splice(idx, 1)
  else perms.push(p)
  selected.value = { ...selected.value, permissions: perms }
  // update in list
  const ri = roles.value.findIndex(r => r.name === selected.value!.name)
  if (ri >= 0) roles.value[ri] = selected.value
}

async function savePerms() {
  if (!selected.value) return
  try {
    await apif('/rbac/roles/' + selected.value.name + '/permissions', { permissions: selected.value.permissions })
    app.showToast('Permissões salvas', 'ok')
  } catch { app.showToast('Erro ao salvar', 'err') }
}

function openModal(role?: Role) {
  editRole.value = role ?? null
  form.value = { name: role?.name ?? '', description: role?.description ?? '' }
  modalOpen.value = true
}

async function saveRole() {
  try {
    await apif('/rbac/roles', form.value)
    app.showToast('Role salvo', 'ok')
    modalOpen.value = false
    load()
  } catch { app.showToast('Erro ao salvar role', 'err') }
}

async function deleteRole(name: string) {
  if (!confirm(`Remover role "${name}"?`)) return
  try {
    await api('/rbac/roles/' + name, { method: 'DELETE' })
    app.showToast('Role removido', 'ok')
    selected.value = null
    load()
  } catch { app.showToast('Erro ao remover', 'err') }
}

async function load() {
  try {
    const d = await api<{ roles: Role[] }>('/rbac/roles')
    roles.value = d.roles ?? []
  } catch {
    roles.value = [
      { name: 'admin', description: 'Acesso total', system: true, permissions: ['tasks:read','tasks:write','tasks:cancel','tasks:delete','agents:read','agents:kill','agents:configure','admin:read','admin:write','admin:rbac','admin:chaos','data:read','data:write','data:export','secrets:read','secrets:write','redis:read','redis:flush','db:query','migrations:run','chaos:run'] },
      { name: 'operator', description: 'Operações do NOC', system: false, permissions: ['tasks:read','tasks:write','tasks:cancel','agents:read','data:read','redis:read','db:query'] },
      { name: 'viewer', description: 'Somente leitura', system: false, permissions: ['tasks:read','agents:read','data:read'] },
      { name: 'analyst', description: 'Análise de dados', system: false, permissions: ['tasks:read','data:read','data:export'] }
    ]
  }
}

onMounted(load)
</script>

<style scoped>
.rbac-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.mb-2 { margin-bottom: 8px; }
.mb-4 { margin-bottom: 16px; }
.mt-4 { margin-top: 16px; }

.rbac-layout {
  display: grid;
  grid-template-columns: 260px 1fr;
  gap: 12px;
  align-items: start;
}

.roles-panel { padding: 12px; }
.role-search { }

.role-item {
  padding: 10px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  transition: var(--transition);
  border: 1px solid transparent;
  margin-bottom: 4px;
}
.role-item:hover { background: var(--bg3); }
.role-item.selected { background: rgba(0,212,255,0.06); border-color: rgba(0,212,255,0.2); }
.ri-name { font-weight: 600; font-size: 13px; margin-bottom: 4px; }
.ri-meta { margin-top: 4px; }

.perms-panel { padding: 16px; }
.no-selection { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 300px; gap: 12px; }
.role-title { font-size: 16px; font-weight: 700; }

.perms-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 16px;
}

.perm-section { }
.ps-label {
  font-size: 10px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--accent);
  margin-bottom: 8px;
  padding-bottom: 4px;
  border-bottom: 1px solid var(--panel-border);
}

.perm-row { margin-bottom: 6px; }
.perm-check {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 12px;
}
.perm-check input { width: auto; cursor: pointer; accent-color: var(--accent); }
.perm-name { color: var(--text); }

/* Modal */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 500;
  backdrop-filter: blur(4px);
}
.modal-box { width: 400px; padding: 20px; }
.modal-title { font-size: 15px; font-weight: 700; }

@media (max-width: 900px) {
  .rbac-layout { grid-template-columns: 1fr; }
}
</style>
