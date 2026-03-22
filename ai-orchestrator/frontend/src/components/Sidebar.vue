<template>
  <nav class="app-sidebar" :class="{ open: app.sidebarOpen }">
    <!-- Search -->
    <div class="sb-search-wrap">
      <input
        v-model="query"
        class="sb-search"
        placeholder="Filtrar..."
        @input="onSearch"
      />
    </div>

    <!-- Pinned / NOC essentials -->
    <div class="sb-section">
      <SbItem v-for="r in pinned" :key="r.path" :route="r" :active="isActive(r.path)" />
    </div>

    <div class="sb-divider" />

    <!-- Collapsible groups -->
    <template v-for="g in groups" :key="g.id">
      <button
        class="sb-group-header"
        :class="{ open: openGroups.has(g.id) }"
        @click="toggleGroup(g.id)"
      >
        <span>{{ g.label }}</span>
        <span class="sb-arr">{{ openGroups.has(g.id) ? '▾' : '▸' }}</span>
      </button>
      <template v-if="openGroups.has(g.id)">
        <SbItem
          v-for="r in g.routes"
          :key="r.path"
          :route="r"
          :active="isActive(r.path)"
          class="sb-child"
        />
      </template>
    </template>
  </nav>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'
import { useRoute } from 'vue-router'
import { useAppStore } from '@/stores/app'
import { router } from '@/router'
import SbItem from './SbItem.vue'

const app = useAppStore()
const route = useRoute()
const query = ref('')
const openGroups = ref<Set<string>>(new Set())

function isActive(path: string) {
  return route.path === path
}

function toggleGroup(id: string) {
  if (openGroups.value.has(id)) openGroups.value.delete(id)
  else openGroups.value.add(id)
}

function onSearch() {
  if (query.value.trim()) {
    // Open all groups so filtered items appear
    for (const g of groups.value) openGroups.value.add(g.id)
  }
}

const allRoutes = router.getRoutes()

function routesByGroup(groupId: string) {
  return allRoutes
    .filter(r => r.meta?.group === groupId)
    .filter(r => !PINNED_PATHS.includes(r.path))  // don't duplicate pinned items
    .filter(r => !query.value || r.meta?.label?.toString().toLowerCase().includes(query.value.toLowerCase()))
}

const PINNED_PATHS = ['/noc', '/noc/logs', '/noc/kanban', '/noc/agents', '/noc/ask', '/noc/metrics', '/noc/engine']

const pinned = computed(() => {
  return allRoutes
    .filter(r => PINNED_PATHS.includes(r.path))
    .filter(r => !query.value || r.meta?.label?.toString().toLowerCase().includes(query.value.toLowerCase()))
    .sort((a, b) => PINNED_PATHS.indexOf(a.path) - PINNED_PATHS.indexOf(b.path))
})

const groupDefs = [
  { id: 'exec', label: 'Execução' },
  { id: 'intel', label: 'Inteligência' },
  { id: 'llm', label: 'LLM Ops' },
  { id: 'infra', label: 'Infraestrutura' },
  { id: 'obs', label: 'Observabilidade' },
  { id: 'know', label: 'Conhecimento' },
  { id: 'sec', label: 'Segurança' },
  { id: 'multi', label: 'Multi-tenancy' },
  { id: 'finops', label: 'FinOps' },
  { id: 'ops', label: 'Operações' },
  { id: 'dev', label: 'Developer' }
]

const groups = computed(() =>
  groupDefs
    .map(g => ({ ...g, routes: routesByGroup(g.id) }))
    .filter(g => g.routes.length > 0)
)
</script>

<style scoped>
.app-sidebar {
  width: var(--sidebar-w);
  flex-shrink: 0;
  background: var(--bg2);
  border-right: 1px solid var(--panel-border);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  overflow-x: hidden;
  transition: width var(--transition);
}

.app-sidebar:not(.open) {
  width: 0;
}

.sb-search-wrap {
  padding: 8px;
  flex-shrink: 0;
}
.sb-search {
  font-size: 12px;
  padding: 5px 8px;
  background: var(--bg3);
  border-color: var(--panel-border);
}

.sb-section {
  padding: 4px 0;
}

.sb-divider {
  height: 1px;
  background: var(--panel-border);
  margin: 4px 8px;
}

.sb-group-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 6px 12px;
  font-size: 10px;
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.6px;
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition);
  text-align: left;
}
.sb-group-header:hover { color: var(--text); background: var(--panel); }
.sb-group-header.open { color: var(--accent); }

.sb-arr { font-size: 9px; }

.sb-child :deep(.sb-item) {
  padding-left: 22px;
}
</style>
