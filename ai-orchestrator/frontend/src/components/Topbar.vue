<template>
  <header class="topbar">
    <div class="topbar-left">
      <button class="sidebar-toggle" @click="app.toggleSidebar()" title="Toggle Sidebar (⌘\\)">
        <span class="tb-icon">☰</span>
      </button>
      <span class="topbar-brand">
        <span class="brand-hex">⬡</span>
        <span class="brand-name">N5<span class="brand-sub"> NOC</span></span>
      </span>
    </div>

    <div class="topbar-center">
      <div class="tenant-selector">
        <span class="ts-label">tenant</span>
        <select :value="app.tenantId" @change="app.setTenant(($event.target as HTMLSelectElement).value)">
          <option value="default">default</option>
          <option value="acme">acme</option>
          <option value="demo">demo</option>
        </select>
      </div>

      <button class="cmd-hint" @click="app.openCommandPalette()">
        <span>⌘K</span>
        <span class="cmd-hint-label">Search</span>
      </button>
    </div>

    <div class="topbar-right">
      <span class="topbar-clock mono">{{ clock }}</span>
      <span class="status-dot" :class="connStatus" :title="connStatus"></span>
    </div>
  </header>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from 'vue'
import { useAppStore } from '@/stores/app'

const app = useAppStore()
const clock = ref('')
const connStatus = ref('ok')

let clockInterval: ReturnType<typeof setInterval>

function tick() {
  const now = new Date()
  clock.value = now.toLocaleTimeString('pt-BR', { hour12: false })
}

onMounted(() => {
  tick()
  clockInterval = setInterval(tick, 1000)
})

onUnmounted(() => clearInterval(clockInterval))
</script>

<style scoped>
.topbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  height: var(--topbar-h);
  padding: 0 12px 0 8px;
  background: var(--bg2);
  border-bottom: 1px solid var(--panel-border);
  flex-shrink: 0;
  gap: 12px;
  z-index: 100;
}

.topbar-left, .topbar-right, .topbar-center {
  display: flex;
  align-items: center;
  gap: 10px;
}

.sidebar-toggle {
  width: 32px;
  height: 32px;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--radius-sm);
  color: var(--text-muted);
  transition: var(--transition);
}
.sidebar-toggle:hover { background: var(--panel); color: var(--text); }

.topbar-brand {
  display: flex;
  align-items: center;
  gap: 6px;
  font-weight: 700;
  font-size: 15px;
}
.brand-hex { color: var(--accent); font-size: 18px; }
.brand-name { letter-spacing: -0.5px; }
.brand-sub { color: var(--text-muted); font-weight: 400; font-size: 12px; }

.tenant-selector {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--panel);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  padding: 4px 8px;
}
.ts-label { color: var(--text-muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; }
.tenant-selector select {
  background: transparent;
  border: none;
  padding: 0;
  font-size: 12px;
  width: auto;
  cursor: pointer;
}

.cmd-hint {
  display: flex;
  align-items: center;
  gap: 6px;
  background: var(--panel);
  border: 1px solid var(--panel-border);
  border-radius: var(--radius-sm);
  padding: 4px 10px;
  font-size: 11px;
  color: var(--text-muted);
  transition: var(--transition);
}
.cmd-hint:hover { border-color: var(--accent); color: var(--accent); }
.cmd-hint > span:first-child {
  background: var(--bg3);
  border-radius: 3px;
  padding: 1px 5px;
  font-family: var(--font-mono);
  font-size: 10px;
}

.topbar-clock {
  font-size: 12px;
  color: var(--text-muted);
}

.status-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--text-muted);
}
.status-dot.ok { background: var(--accent3); box-shadow: 0 0 6px var(--accent3); }
.status-dot.warn { background: var(--warn); }
.status-dot.err { background: var(--danger); }
</style>
