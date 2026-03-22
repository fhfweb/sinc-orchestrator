<template>
  <div class="chaos-page">
    <div class="page-header flex justify-between items-center">
      <div>
        <h1 class="page-title">⚡ Chaos Engineering</h1>
        <p class="text-muted" style="font-size:12px">Injeção controlada de falhas para validar resiliência</p>
      </div>
      <div class="chaos-status card flex items-center gap-2" style="padding:8px 14px">
        <span class="dot" :class="activeExperiment ? 'err' : 'ok'"></span>
        <span>{{ activeExperiment ? 'Experimento ativo' : 'Sistema estável' }}</span>
      </div>
    </div>

    <!-- Experiment log -->
    <div class="chaos-grid">
      <!-- Actions panel -->
      <div class="actions-panel">
        <!-- Kill Agent -->
        <div class="card chaos-action">
          <div class="ca-head">
            <span class="ca-icon">☠</span>
            <div>
              <div class="ca-title">Kill Agent</div>
              <div class="text-muted" style="font-size:11px">Termina um agente em execução</div>
            </div>
          </div>
          <select v-model="killTarget" class="mt-2">
            <option value="">Selecione agente...</option>
            <option v-for="a in agents" :key="a.id" :value="a.id">{{ a.name }}</option>
          </select>
          <button class="btn btn-danger mt-2" :disabled="!killTarget || !!running" @click="killAgent">
            {{ running === 'kill' ? '…' : 'Kill Agent' }}
          </button>
        </div>

        <!-- Inject Delay -->
        <div class="card chaos-action">
          <div class="ca-head">
            <span class="ca-icon">⏱</span>
            <div>
              <div class="ca-title">Inject Delay</div>
              <div class="text-muted" style="font-size:11px">Adiciona latência artificial</div>
            </div>
          </div>
          <div class="flex gap-2 mt-2 items-center">
            <input v-model.number="delayMs" type="number" placeholder="ms" style="width:90px" />
            <span class="text-muted">ms por {{ delayDuration }}s</span>
          </div>
          <input v-model.number="delayDuration" type="range" min="5" max="300" class="mt-2" />
          <button class="btn btn-danger mt-2" :disabled="running === 'delay'" @click="injectDelay">
            {{ running === 'delay' ? '…' : `Injetar ${delayMs}ms` }}
          </button>
        </div>

        <!-- Queue Saturation -->
        <div class="card chaos-action">
          <div class="ca-head">
            <span class="ca-icon">⊛</span>
            <div>
              <div class="ca-title">Queue Saturation</div>
              <div class="text-muted" style="font-size:11px">Satura a fila de tarefas</div>
            </div>
          </div>
          <div class="flex gap-2 mt-2 items-center">
            <input v-model.number="queueCount" type="number" placeholder="nº tasks" style="width:100px" />
            <span class="text-muted">tasks dummy</span>
          </div>
          <button class="btn btn-danger mt-2" :disabled="running === 'queue'" @click="saturateQueue">
            {{ running === 'queue' ? '…' : `Saturar (${queueCount})` }}
          </button>
        </div>

        <!-- Error Rate -->
        <div class="card chaos-action">
          <div class="ca-head">
            <span class="ca-icon">✕</span>
            <div>
              <div class="ca-title">Error Rate</div>
              <div class="text-muted" style="font-size:11px">Força respostas de erro</div>
            </div>
          </div>
          <div class="flex gap-2 mt-2 items-center">
            <input v-model.number="errorPct" type="range" min="0" max="100" />
            <span class="text-warn mono" style="width:38px">{{ errorPct }}%</span>
          </div>
          <button class="btn btn-danger mt-2" :disabled="running === 'err'" @click="injectErrors">
            {{ running === 'err' ? '…' : `Injetar ${errorPct}% erros` }}
          </button>
        </div>
      </div>

      <!-- Log panel -->
      <div class="card log-panel">
        <div class="card-header flex justify-between items-center">
          <span class="card-title">Log de Experimentos</span>
          <button class="btn btn-ghost" style="font-size:11px" @click="log = []">limpar</button>
        </div>
        <div class="chaos-log mono" ref="logRef">
          <div v-if="log.length === 0" class="text-muted" style="padding:16px;text-align:center">
            Nenhum experimento executado ainda.
          </div>
          <div v-for="(entry, i) in log" :key="i" class="log-entry" :class="`type-${entry.type}`">
            <span class="le-ts">{{ entry.ts }}</span>
            <span class="le-type">{{ entry.type.toUpperCase() }}</span>
            <span class="le-msg">{{ entry.msg }}</span>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, nextTick } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api, apif } = useApi()
const app = useAppStore()

interface Agent { id: string; name: string }
interface LogEntry { ts: string; type: string; msg: string }

const agents = ref<Agent[]>([])
const killTarget = ref('')
const delayMs = ref(500)
const delayDuration = ref(30)
const queueCount = ref(50)
const errorPct = ref(20)
const running = ref('')
const activeExperiment = ref(false)
const log = ref<LogEntry[]>([])
const logRef = ref<HTMLElement>()

function addLog(type: string, msg: string) {
  const ts = new Date().toLocaleTimeString('pt-BR', { hour12: false })
  log.value.unshift({ ts, type, msg })
  if (log.value.length > 200) log.value.pop()
}

async function killAgent() {
  running.value = 'kill'
  activeExperiment.value = true
  try {
    await apif('/chaos/kill-agent', { agent_id: killTarget.value })
    addLog('kill', `Agente ${killTarget.value} encerrado`)
    app.showToast('Agente morto', 'warn')
  } catch (e: unknown) {
    addLog('err', `Kill falhou: ${e instanceof Error ? e.message : String(e)}`)
    app.showToast('Falha no kill', 'err')
  } finally {
    running.value = ''
    setTimeout(() => { activeExperiment.value = false }, 3000)
  }
}

async function injectDelay() {
  running.value = 'delay'
  activeExperiment.value = true
  try {
    await apif('/chaos/inject-delay', { delay_ms: delayMs.value, duration_s: delayDuration.value })
    addLog('delay', `Delay ${delayMs.value}ms injetado por ${delayDuration.value}s`)
    app.showToast(`Delay ${delayMs.value}ms ativo por ${delayDuration.value}s`, 'warn')
    setTimeout(() => { activeExperiment.value = false }, delayDuration.value * 1000)
  } catch (e: unknown) {
    addLog('err', `Delay falhou: ${e instanceof Error ? e.message : String(e)}`)
    activeExperiment.value = false
  } finally {
    running.value = ''
  }
}

async function saturateQueue() {
  running.value = 'queue'
  activeExperiment.value = true
  try {
    await apif('/chaos/saturate-queue', { count: queueCount.value })
    addLog('queue', `${queueCount.value} tasks dummy injetadas na fila`)
    app.showToast(`Fila saturada com ${queueCount.value} tasks`, 'warn')
  } catch (e: unknown) {
    addLog('err', `Saturação falhou: ${e instanceof Error ? e.message : String(e)}`)
  } finally {
    running.value = ''
    setTimeout(() => { activeExperiment.value = false }, 5000)
  }
}

async function injectErrors() {
  running.value = 'err'
  activeExperiment.value = true
  try {
    await apif('/chaos/error-rate', { error_pct: errorPct.value })
    addLog('error', `Taxa de erro ${errorPct.value}% ativada`)
    app.showToast(`${errorPct.value}% erros injetados`, 'warn')
  } catch (e: unknown) {
    addLog('err', `Injeção falhou: ${e instanceof Error ? e.message : String(e)}`)
    activeExperiment.value = false
  } finally {
    running.value = ''
  }
}

onMounted(async () => {
  try {
    const d = await api<{ agents: Agent[] }>('/agents/roster')
    agents.value = d.agents ?? []
  } catch {
    agents.value = [
      { id: 'orch-0001', name: 'Orchestrator' },
      { id: 'rag-0001', name: 'RAG-Engine' },
      { id: 'llm-0001', name: 'LLM-Router' },
      { id: 'cog-0001', name: 'Cognitive-Core' }
    ]
  }
})
</script>

<style scoped>
.chaos-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.chaos-status { font-size: 12px; }

.chaos-grid {
  display: grid;
  grid-template-columns: 380px 1fr;
  gap: 12px;
  align-items: start;
}

.actions-panel { display: flex; flex-direction: column; gap: 10px; }

.chaos-action { padding: 14px; }
.ca-head { display: flex; align-items: flex-start; gap: 10px; }
.ca-icon { font-size: 20px; color: var(--danger); width: 28px; text-align: center; }
.ca-title { font-size: 13px; font-weight: 600; }

.log-panel { display: flex; flex-direction: column; }
.card-header { margin-bottom: 8px; }
.card-title { font-size: 12px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }

.chaos-log {
  flex: 1;
  overflow-y: auto;
  max-height: 520px;
  font-size: 11.5px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.log-entry {
  display: flex;
  gap: 10px;
  padding: 4px 6px;
  border-radius: 3px;
  align-items: baseline;
}
.log-entry:hover { background: var(--bg3); }
.le-ts { color: var(--text-dim); flex-shrink: 0; width: 72px; }
.le-type { width: 52px; flex-shrink: 0; font-weight: 700; font-size: 10px; }
.le-msg { flex: 1; }

.type-kill .le-type { color: var(--danger); }
.type-delay .le-type { color: var(--warn); }
.type-queue .le-type { color: var(--accent2); }
.type-error .le-type { color: var(--danger); }
.type-err .le-type { color: var(--danger); }

@media (max-width: 900px) {
  .chaos-grid { grid-template-columns: 1fr; }
}
</style>
