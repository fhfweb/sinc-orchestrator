import { defineStore } from 'pinia'
import { ref, computed } from 'vue'

export const useAppStore = defineStore('app', () => {
  const tenantId = ref<string>(localStorage.getItem('tenantId') || 'default')
  const theme = ref<'dark' | 'light'>('dark')
  const sidebarOpen = ref(true)
  const commandPaletteOpen = ref(false)
  const toast = ref<{ msg: string; type: 'ok' | 'warn' | 'err' | 'info' } | null>(null)
  let toastTimer: ReturnType<typeof setTimeout> | null = null

  const apiBase = computed(() => `/api/v5/dashboard`)

  function setTenant(id: string) {
    tenantId.value = id
    localStorage.setItem('tenantId', id)
  }

  function toggleSidebar() {
    sidebarOpen.value = !sidebarOpen.value
  }

  function showToast(msg: string, type: 'ok' | 'warn' | 'err' | 'info' = 'info', duration = 3000) {
    toast.value = { msg, type }
    if (toastTimer) clearTimeout(toastTimer)
    toastTimer = setTimeout(() => { toast.value = null }, duration)
  }

  function openCommandPalette() { commandPaletteOpen.value = true }
  function closeCommandPalette() { commandPaletteOpen.value = false }

  return {
    tenantId,
    theme,
    sidebarOpen,
    commandPaletteOpen,
    toast,
    apiBase,
    setTenant,
    toggleSidebar,
    showToast,
    openCommandPalette,
    closeCommandPalette
  }
})
