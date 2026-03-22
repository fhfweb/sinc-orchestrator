<template>
  <div class="ask-page">
    <div class="ask-messages" ref="messagesRef">
      <div v-if="messages.length === 0" class="ask-welcome">
        <div class="aw-icon">✦</div>
        <h2>Ask N5</h2>
        <p class="text-muted">Faça perguntas sobre seus agentes, tarefas e infraestrutura.</p>
      </div>
      <div
        v-for="(m, i) in messages"
        :key="i"
        class="ask-msg"
        :class="`msg-${m.role}`"
      >
        <div class="msg-role">{{ m.role === 'user' ? 'Você' : 'N5' }}</div>
        <div class="msg-body" v-html="formatMsg(m.content)"></div>
      </div>
      <div v-if="loading" class="ask-msg msg-assistant">
        <div class="msg-role">N5</div>
        <div class="msg-body thinking">
          <span></span><span></span><span></span>
        </div>
      </div>
    </div>

    <div class="ask-input-area card">
      <textarea
        v-model="input"
        class="ask-textarea"
        placeholder="Pergunte algo ao N5..."
        rows="3"
        @keydown.enter.exact.prevent="send"
        @keydown.enter.shift.exact.stop
      ></textarea>
      <div class="ask-input-footer flex justify-between items-center">
        <span class="text-muted" style="font-size:11px">Enter para enviar · Shift+Enter nova linha</span>
        <button class="btn btn-primary" :disabled="loading || !input.trim()" @click="send">
          {{ loading ? '…' : 'Enviar ✦' }}
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { apif } = useApi()
const app = useAppStore()

interface Msg { role: 'user' | 'assistant'; content: string }

const messages = ref<Msg[]>([])
const input = ref('')
const loading = ref(false)
const messagesRef = ref<HTMLElement>()

function scrollBottom() {
  nextTick(() => {
    if (messagesRef.value) messagesRef.value.scrollTop = messagesRef.value.scrollHeight
  })
}

function formatMsg(text: string) {
  return text
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/```([\s\S]*?)```/g, '<pre class="mono">$1</pre>')
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\n/g, '<br>')
}

async function send() {
  const q = input.value.trim()
  if (!q || loading.value) return
  messages.value.push({ role: 'user', content: q })
  input.value = ''
  loading.value = true
  scrollBottom()

  try {
    const data = await apif<{ answer?: string; response?: string }>('/ask', { question: q })
    const answer = data.answer ?? data.response ?? 'Sem resposta.'
    messages.value.push({ role: 'assistant', content: answer })
  } catch (e: unknown) {
    const err = e instanceof Error ? e.message : 'Erro desconhecido'
    messages.value.push({ role: 'assistant', content: `Erro: ${err}` })
  } finally {
    loading.value = false
    scrollBottom()
  }
}
</script>

<style scoped>
.ask-page {
  display: flex;
  flex-direction: column;
  height: calc(100vh - var(--topbar-h) - 40px);
  gap: 12px;
}

.ask-messages {
  flex: 1;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding: 8px 0;
}

.ask-welcome {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  flex: 1;
  text-align: center;
  gap: 10px;
}
.aw-icon { font-size: 48px; color: var(--accent); opacity: 0.4; }
.ask-welcome h2 { font-size: 20px; font-weight: 700; }

.ask-msg { display: flex; flex-direction: column; gap: 4px; max-width: 80%; }
.msg-user { align-self: flex-end; }
.msg-assistant { align-self: flex-start; }

.msg-role { font-size: 10px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; color: var(--text-muted); }
.msg-user .msg-role { text-align: right; }

.msg-body {
  padding: 10px 14px;
  border-radius: var(--radius);
  font-size: 13px;
  line-height: 1.6;
  background: var(--panel);
  border: 1px solid var(--panel-border);
}
.msg-user .msg-body { background: rgba(0,212,255,0.08); border-color: rgba(0,212,255,0.2); }
.msg-body :deep(pre) {
  background: var(--bg3);
  padding: 8px;
  border-radius: 4px;
  overflow-x: auto;
  margin: 8px 0;
  font-size: 11.5px;
}
.msg-body :deep(code) {
  background: var(--bg3);
  padding: 2px 5px;
  border-radius: 3px;
  font-size: 11.5px;
}

/* Thinking dots */
.thinking {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 12px 14px;
}
.thinking span {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--accent);
  animation: blink 1.2s infinite;
}
.thinking span:nth-child(2) { animation-delay: 0.2s; }
.thinking span:nth-child(3) { animation-delay: 0.4s; }
@keyframes blink { 0%,80%,100% { opacity:0.2; } 40% { opacity:1; } }

.ask-input-area { flex-shrink: 0; display: flex; flex-direction: column; gap: 8px; }
.ask-textarea {
  resize: none;
  border: none;
  padding: 8px 4px;
  font-size: 13px;
  background: transparent;
}
.ask-textarea:focus { box-shadow: none; border: none; }
</style>
