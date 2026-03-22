<template>
  <div class="cp-overlay" @click.self="app.closeCommandPalette()">
    <div class="cp-panel">
      <div class="cp-search-wrap">
        <span class="cp-icon">⌘</span>
        <input
          ref="inputRef"
          v-model="query"
          class="cp-input"
          placeholder="Navegar para..."
          @keydown.escape="app.closeCommandPalette()"
          @keydown.arrow-down.prevent="moveSelection(1)"
          @keydown.arrow-up.prevent="moveSelection(-1)"
          @keydown.enter.prevent="goSelected"
        />
        <kbd class="cp-esc">ESC</kbd>
      </div>

      <ul class="cp-results" ref="listRef">
        <li
          v-for="(r, i) in results"
          :key="r.path"
          class="cp-result"
          :class="{ selected: i === selectedIdx }"
          @click="go(r.path)"
          @mouseenter="selectedIdx = i"
        >
          <span class="cp-r-icon">{{ r.meta?.icon ?? '◈' }}</span>
          <span class="cp-r-label">{{ r.meta?.label ?? r.path }}</span>
          <span class="cp-r-path">{{ r.path }}</span>
        </li>
        <li v-if="results.length === 0" class="cp-empty">Nenhum resultado</li>
      </ul>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, nextTick } from 'vue'
import { useRouter, useRoute } from 'vue-router'
import { useAppStore } from '@/stores/app'

const app = useAppStore()
const router = useRouter()
const query = ref('')
const selectedIdx = ref(0)
const inputRef = ref<HTMLInputElement>()

onMounted(() => nextTick(() => inputRef.value?.focus()))

const allRoutes = router.getRoutes().filter(r => r.meta?.label)

const results = computed(() => {
  const q = query.value.toLowerCase()
  if (!q) return allRoutes.slice(0, 20)
  return allRoutes.filter(r =>
    r.meta?.label?.toString().toLowerCase().includes(q) ||
    r.path.toLowerCase().includes(q)
  ).slice(0, 12)
})

function moveSelection(delta: number) {
  selectedIdx.value = Math.max(0, Math.min(results.value.length - 1, selectedIdx.value + delta))
}

function go(path: string) {
  router.push(path)
  app.closeCommandPalette()
}

function goSelected() {
  const r = results.value[selectedIdx.value]
  if (r) go(r.path)
}
</script>

<style scoped>
.cp-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex;
  align-items: flex-start;
  justify-content: center;
  padding-top: 15vh;
  z-index: 1000;
  backdrop-filter: blur(4px);
}

.cp-panel {
  width: 540px;
  max-height: 60vh;
  background: var(--bg2);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius);
  box-shadow: var(--shadow), var(--glow);
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.cp-search-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--panel-border);
}

.cp-icon { color: var(--accent); font-size: 16px; flex-shrink: 0; }

.cp-input {
  flex: 1;
  border: none;
  background: transparent;
  font-size: 15px;
  padding: 0;
  color: var(--text);
}

.cp-esc {
  font-size: 10px;
  padding: 2px 6px;
  background: var(--bg3);
  border-radius: 3px;
  color: var(--text-muted);
  font-family: var(--font-mono);
}

.cp-results {
  list-style: none;
  overflow-y: auto;
  max-height: 50vh;
}

.cp-result {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 14px;
  cursor: pointer;
  transition: var(--transition);
  border-bottom: 1px solid rgba(255,255,255,0.03);
}
.cp-result:hover, .cp-result.selected {
  background: rgba(0,212,255,0.08);
}

.cp-r-icon { width: 18px; text-align: center; color: var(--accent); }
.cp-r-label { flex: 1; font-size: 13px; }
.cp-r-path { font-size: 11px; color: var(--text-dim); font-family: var(--font-mono); }

.cp-empty {
  padding: 20px;
  text-align: center;
  color: var(--text-muted);
  font-size: 13px;
}
</style>
