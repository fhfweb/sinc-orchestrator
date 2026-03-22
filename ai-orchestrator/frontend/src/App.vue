<template>
  <div class="app-shell" :class="{ 'sidebar-closed': !app.sidebarOpen }">
    <Topbar />
    <div class="app-body">
      <Sidebar />
      <main class="app-main">
        <RouterView v-slot="{ Component, route }">
          <Transition name="fade" mode="out-in">
            <component :is="Component" :key="route.path" />
          </Transition>
        </RouterView>
      </main>
    </div>
    <Toast v-if="app.toast" :msg="app.toast.msg" :type="app.toast.type" />
    <CommandPalette v-if="app.commandPaletteOpen" />
  </div>
</template>

<script setup lang="ts">
import { onMounted, onUnmounted } from 'vue'
import { useAppStore } from '@/stores/app'
import Topbar from '@/components/Topbar.vue'
import Sidebar from '@/components/Sidebar.vue'
import Toast from '@/components/Toast.vue'
import CommandPalette from '@/components/CommandPalette.vue'

const app = useAppStore()

function handleKeydown(e: KeyboardEvent) {
  if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
    e.preventDefault()
    if (app.commandPaletteOpen) app.closeCommandPalette()
    else app.openCommandPalette()
  }
  if (e.key === 'Escape' && app.commandPaletteOpen) {
    app.closeCommandPalette()
  }
}

onMounted(() => document.addEventListener('keydown', handleKeydown))
onUnmounted(() => document.removeEventListener('keydown', handleKeydown))
</script>

<style>
.app-shell {
  display: flex;
  flex-direction: column;
  height: 100vh;
  overflow: hidden;
}

.app-body {
  display: flex;
  flex: 1;
  overflow: hidden;
}

.app-main {
  flex: 1;
  overflow-y: auto;
  overflow-x: hidden;
  padding: 20px;
  background: var(--bg);
}

/* Sidebar toggle */
.app-shell.sidebar-closed .app-sidebar {
  width: 0;
  overflow: hidden;
}

.fade-enter-active,
.fade-leave-active {
  transition: opacity 0.12s ease;
}
.fade-enter-from,
.fade-leave-to {
  opacity: 0;
}
</style>
