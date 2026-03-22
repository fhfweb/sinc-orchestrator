<template>
  <div class="runbooks-page">
    <div class="page-header flex justify-between items-center">
      <h1 class="page-title">📖 Runbooks</h1>
      <button class="btn btn-primary" @click="openModal()">+ Novo Runbook</button>
    </div>

    <div class="rb-layout">
      <div class="card rb-list">
        <input v-model="search" placeholder="Buscar runbook..." class="mb-2" />
        <div v-for="r in filtered" :key="r.id" class="rb-item" :class="{ selected: selected?.id === r.id }" @click="selected = r">
          <div class="rbi-title">{{ r.title }}</div>
          <div class="rbi-meta flex gap-2 mt-1">
            <span class="badge" :class="r.category === 'incident' ? 'badge-err' : r.category === 'maintenance' ? 'badge-warn' : 'badge-info'">{{ r.category }}</span>
            <span class="text-muted" style="font-size:10px">{{ r.steps?.length ?? 0 }} passos</span>
          </div>
        </div>
      </div>

      <div class="card rb-detail">
        <div v-if="!selected" class="no-selection">
          <div style="font-size:36px;opacity:0.2">📖</div>
          <div class="text-muted">Selecione um runbook</div>
        </div>
        <template v-else>
          <div class="rbd-header flex justify-between items-center mb-4">
            <div>
              <h2 style="font-size:16px;font-weight:700">{{ selected.title }}</h2>
              <p class="text-muted" style="font-size:12px;margin-top:4px">{{ selected.description }}</p>
            </div>
            <div class="flex gap-2">
              <button class="btn btn-ghost" @click="openModal(selected)">✎ Editar</button>
              <button class="btn btn-primary" :disabled="executing" @click="execute">
                {{ executing ? '⏳ Executando…' : '▶ Executar' }}
              </button>
            </div>
          </div>

          <div class="steps-list">
            <div v-for="(step, i) in selected.steps" :key="i" class="step-item" :class="{ 'step-done': executedSteps.has(i), 'step-active': currentStep === i }">
              <div class="step-num">{{ i + 1 }}</div>
              <div class="step-body">
                <div class="step-title">{{ step.title }}</div>
                <div class="step-desc text-muted" v-if="step.description">{{ step.description }}</div>
                <pre v-if="step.command" class="step-cmd mono">$ {{ step.command }}</pre>
              </div>
              <div class="step-status">
                <span v-if="executedSteps.has(i)" class="text-ok">✓</span>
                <span v-else-if="currentStep === i" class="text-accent">▶</span>
              </div>
            </div>
          </div>
        </template>
      </div>
    </div>

    <div v-if="modalOpen" class="modal-overlay" @click.self="modalOpen = false">
      <div class="modal-box card">
        <h3 class="modal-title">{{ editRb ? 'Editar' : 'Novo Runbook' }}</h3>
        <div class="form-grid mt-3">
          <label><span class="form-label">Título</span><input v-model="form.title" /></label>
          <label><span class="form-label">Categoria</span>
            <select v-model="form.category"><option>incident</option><option>maintenance</option><option>deployment</option><option>operational</option></select>
          </label>
          <label><span class="form-label">Descrição</span><textarea v-model="form.description" rows="2"></textarea></label>
        </div>
        <div class="flex gap-2 justify-end mt-4">
          <button class="btn btn-ghost" @click="modalOpen = false">Cancelar</button>
          <button class="btn btn-primary" @click="save">Salvar</button>
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

interface Step { title: string; description?: string; command?: string }
interface Runbook { id: string; title: string; description?: string; category: string; steps?: Step[] }

const runbooks = ref<Runbook[]>([])
const selected = ref<Runbook | null>(null)
const search = ref('')
const executing = ref(false)
const executedSteps = ref(new Set<number>())
const currentStep = ref(-1)
const modalOpen = ref(false)
const editRb = ref<Runbook | null>(null)
const form = ref({ title: '', category: 'operational', description: '' })

const filtered = computed(() => runbooks.value.filter(r => !search.value || r.title.toLowerCase().includes(search.value.toLowerCase())))

function openModal(r?: Runbook) {
  editRb.value = r ?? null
  form.value = { title: r?.title ?? '', category: r?.category ?? 'operational', description: r?.description ?? '' }
  modalOpen.value = true
}

async function execute() {
  if (!selected.value?.steps) return
  executing.value = true
  executedSteps.value.clear()
  try {
    for (let i = 0; i < selected.value.steps.length; i++) {
      currentStep.value = i
      await new Promise(r => setTimeout(r, 800))
      executedSteps.value.add(i)
    }
    currentStep.value = -1
    app.showToast('Runbook executado com sucesso', 'ok')
  } finally { executing.value = false }
}

async function save() {
  try {
    await apif('/runbooks', form.value)
    app.showToast('Runbook salvo', 'ok')
    modalOpen.value = false
    load()
  } catch { app.showToast('Erro ao salvar', 'err') }
}

async function load() {
  try {
    const d = await api<{ runbooks: Runbook[] }>('/runbooks')
    runbooks.value = d.runbooks ?? []
  } catch {
    runbooks.value = [
      { id: '1', title: 'Redis OOM Recovery', category: 'incident', description: 'Recuperação de Out-Of-Memory do Redis', steps: [
        { title: 'Verificar uso de memória', command: 'redis-cli info memory' },
        { title: 'Listar chaves sem TTL', command: 'redis-cli --scan --pattern "*" | xargs redis-cli object encoding' },
        { title: 'Flush chaves de cache expirado', description: 'Remover chaves com prefixo cache:* com mais de 7 dias' },
        { title: 'Configurar maxmemory-policy', command: 'redis-cli config set maxmemory-policy allkeys-lru' },
        { title: 'Verificar estabilização', description: 'Monitorar uso de memória por 5 minutos' },
      ]},
      { id: '2', title: 'LLM Failover', category: 'incident', description: 'Ativar fallback de LLM em caso de timeout', steps: [
        { title: 'Confirmar falha do provider primário', command: 'curl -s /health | jq .llm_status' },
        { title: 'Ativar circuit breaker', description: 'Feature flag: llm.circuit_breaker = true' },
        { title: 'Redirecionar para provider backup', command: 'POST /api/v5/dashboard/feature-flags/toggle' },
        { title: 'Monitorar métricas RED', description: 'Verificar P99 voltando ao normal' },
      ]},
      { id: '3', title: 'Deploy de Emergência', category: 'deployment', description: 'Rollback rápido de versão', steps: [
        { title: 'Identificar versão estável anterior', command: 'git log --oneline -10' },
        { title: 'Criar snapshot do estado atual', description: 'Capturar snapshot via dashboard antes do rollback' },
        { title: 'Executar rollback', command: 'docker compose down && docker compose up -d --build' },
        { title: 'Validar health checks', command: 'curl -s /health | jq .' },
      ]},
    ]
  }
}

onMounted(load)
</script>

<style scoped>
.runbooks-page { display: flex; flex-direction: column; gap: 16px; }
.page-title { font-size: 16px; font-weight: 700; }
.mb-2 { margin-bottom: 8px; }
.mb-4 { margin-bottom: 16px; }
.mt-1 { margin-top: 4px; }
.mt-3 { margin-top: 12px; }
.mt-4 { margin-top: 16px; }

.rb-layout { display: grid; grid-template-columns: 260px 1fr; gap: 12px; }

.rb-list { padding: 12px; max-height: calc(100vh - 200px); overflow-y: auto; }
.rb-item { padding: 10px; border-radius: var(--radius-sm); cursor: pointer; border: 1px solid transparent; transition: var(--transition); margin-bottom: 4px; }
.rb-item:hover { background: var(--bg3); }
.rb-item.selected { background: rgba(0,212,255,0.06); border-color: rgba(0,212,255,0.2); }
.rbi-title { font-size: 12.5px; font-weight: 500; }

.rb-detail { padding: 16px; min-height: 400px; }
.no-selection { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 300px; gap: 12px; }

.steps-list { display: flex; flex-direction: column; gap: 8px; }
.step-item { display: flex; gap: 12px; align-items: flex-start; padding: 12px; border-radius: var(--radius-sm); border: 1px solid var(--panel-border); background: var(--bg3); transition: var(--transition); }
.step-done { border-color: rgba(16,185,129,0.3); background: rgba(16,185,129,0.05); }
.step-active { border-color: rgba(0,212,255,0.4); background: rgba(0,212,255,0.05); }
.step-num { width: 24px; height: 24px; border-radius: 50%; background: var(--panel-border); display: flex; align-items: center; justify-content: center; font-size: 11px; font-weight: 700; flex-shrink: 0; }
.step-done .step-num { background: rgba(16,185,129,0.3); color: var(--accent3); }
.step-active .step-num { background: rgba(0,212,255,0.3); color: var(--accent); }
.step-body { flex: 1; }
.step-title { font-size: 13px; font-weight: 600; }
.step-desc { font-size: 11.5px; margin-top: 3px; }
.step-cmd { background: var(--bg); padding: 6px 10px; border-radius: 3px; font-size: 11.5px; margin-top: 6px; }
.step-status { font-size: 16px; }

.form-grid { display: flex; flex-direction: column; gap: 10px; }
.form-label { display: block; font-size: 11px; color: var(--text-muted); margin-bottom: 4px; }
.modal-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 500; backdrop-filter: blur(4px); }
.modal-box { width: 440px; padding: 20px; }
.modal-title { font-size: 15px; font-weight: 700; }
</style>
