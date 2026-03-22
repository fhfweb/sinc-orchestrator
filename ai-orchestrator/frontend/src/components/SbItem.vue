<template>
  <RouterLink :to="route.path" class="sb-item" :class="{ active }">
    <span v-if="route.meta?.icon" class="sb-icon">{{ route.meta.icon }}</span>
    <span class="sb-label">{{ route.meta?.label ?? route.path }}</span>
  </RouterLink>
</template>

<script setup lang="ts">
import type { RouteRecordNormalized } from 'vue-router'

defineProps<{
  route: RouteRecordNormalized
  active?: boolean
}>()
</script>

<style scoped>
.sb-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  font-size: 12.5px;
  color: var(--text-muted);
  cursor: pointer;
  transition: var(--transition);
  text-decoration: none;
  white-space: nowrap;
}
.sb-item:hover {
  color: var(--text);
  background: var(--panel);
  text-decoration: none;
}
.sb-item.active {
  color: var(--accent);
  background: rgba(0, 212, 255, 0.08);
  border-right: 2px solid var(--accent);
}
.sb-icon {
  font-size: 13px;
  width: 16px;
  text-align: center;
  flex-shrink: 0;
}
</style>
