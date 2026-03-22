<template>
  <div class="stub-page">
    <div class="stub-card card">
      <div class="stub-icon">◈</div>
      <h2>{{ label }}</h2>
      <p class="text-muted">Esta página está sendo migrada para Vue 3.</p>
      <p class="text-muted stub-path mono">{{ route.path }}</p>
      <a :href="legacyUrl" class="btn btn-ghost mt-2">Abrir no dashboard legado</a>
    </div>
  </div>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()
const label = computed(() => route.meta?.label as string ?? route.path)
const legacyUrl = computed(() => `/dashboard${route.path.replace('/noc', '')}`)
</script>

<style scoped>
.stub-page {
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 60vh;
}
.stub-card {
  text-align: center;
  max-width: 400px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.stub-icon {
  font-size: 48px;
  color: var(--accent);
  opacity: 0.3;
}
.stub-card h2 { font-size: 18px; font-weight: 600; }
.stub-path { font-size: 11px; color: var(--text-dim); }
</style>
