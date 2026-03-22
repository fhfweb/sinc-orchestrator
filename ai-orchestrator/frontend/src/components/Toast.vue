<template>
  <Transition name="toast">
    <div class="toast" :class="`toast-${type}`">
      <span class="toast-icon">{{ icons[type] }}</span>
      <span>{{ msg }}</span>
    </div>
  </Transition>
</template>

<script setup lang="ts">
defineProps<{
  msg: string
  type: 'ok' | 'warn' | 'err' | 'info'
}>()

const icons = { ok: '✓', warn: '⚠', err: '✕', info: 'ℹ' }
</script>

<style scoped>
.toast {
  position: fixed;
  bottom: 24px;
  right: 24px;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-radius: var(--radius);
  font-size: 13px;
  font-weight: 500;
  background: var(--bg3);
  border: 1px solid var(--panel-border);
  box-shadow: var(--shadow);
  z-index: 9999;
  max-width: 360px;
}
.toast-ok { border-color: var(--accent3); color: var(--accent3); }
.toast-warn { border-color: var(--warn); color: var(--warn); }
.toast-err { border-color: var(--danger); color: var(--danger); }
.toast-info { border-color: var(--accent); color: var(--accent); }

.toast-enter-active { transition: all 0.2s ease; }
.toast-leave-active { transition: all 0.2s ease; }
.toast-enter-from { opacity: 0; transform: translateY(12px); }
.toast-leave-to { opacity: 0; transform: translateY(12px); }
</style>
