<template>
  <div class="capacity-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Predictive Capacity</h1>
        <p class="text-muted">Resource utilization forecasting and scaling recommendations</p>
      </div>
      <button class="btn btn-ghost" @click="loadData" :disabled="loading">
        {{ loading ? '...' : 'Refresh' }}
      </button>
    </div>

    <!-- Current Utilization -->
    <div class="flex gap-2" style="margin-bottom:1.5rem; flex-wrap:wrap;">
      <div
        v-for="res in capacity.current"
        :key="res.name"
        class="card resource-card"
      >
        <div class="resource-name text-muted">{{ res.name }}</div>
        <div class="resource-value" :class="resourceValueClass(res.pct)">{{ res.pct }}%</div>
        <div class="resource-bar-wrap">
          <div
            class="resource-bar-fill"
            :class="resourceBarClass(res.pct)"
            :style="{ width: res.pct + '%' }"
          ></div>
        </div>
        <div class="resource-detail text-muted">{{ res.detail }}</div>
      </div>
    </div>

    <!-- 7-Day Prediction Chart -->
    <div class="card" style="margin-bottom:1.5rem; padding:1.25rem;">
      <div class="chart-title flex items-center justify-between">
        <span>7-Day Load Prediction</span>
        <div class="flex gap-2">
          <span class="legend-dot cpu"></span><span style="font-size:.75rem; color:var(--text-muted);">CPU</span>
          <span class="legend-dot mem"></span><span style="font-size:.75rem; color:var(--text-muted);">Memory</span>
          <span class="legend-dot llm"></span><span style="font-size:.75rem; color:var(--text-muted);">LLM Rate</span>
        </div>
      </div>
      <div class="bar-chart">
        <div class="bar-y-labels">
          <span>100%</span>
          <span>75%</span>
          <span>50%</span>
          <span>25%</span>
          <span>0%</span>
        </div>
        <div class="bar-area">
          <div
            v-for="day in capacity.prediction"
            :key="day.date"
            class="bar-day"
          >
            <div class="stacked-bars">
              <div class="bar-seg cpu-seg" :style="{ height: day.cpu + '%' }" :title="'CPU: ' + day.cpu + '%'"></div>
            </div>
            <div class="stacked-bars">
              <div class="bar-seg mem-seg" :style="{ height: day.memory + '%' }" :title="'Mem: ' + day.memory + '%'"></div>
            </div>
            <div class="stacked-bars">
              <div class="bar-seg llm-seg" :style="{ height: day.llmRate + '%' }" :title="'LLM: ' + day.llmRate + '%'"></div>
            </div>
            <div class="day-label text-muted">{{ day.label }}</div>
          </div>
        </div>
      </div>
      <div class="threshold-line-label text-muted">
        — 80% threshold shown in recommendations below
      </div>
    </div>

    <!-- Recommendations -->
    <div class="card">
      <div class="section-title">Scaling Recommendations</div>
      <div class="recs-list">
        <div
          v-for="rec in capacity.recommendations"
          :key="rec.id"
          class="rec-item"
          :class="'rec-' + rec.priority"
        >
          <div class="flex items-center gap-2">
            <span class="badge" :class="priorityBadge(rec.priority)">{{ rec.priority }}</span>
            <span class="rec-title">{{ rec.title }}</span>
          </div>
          <div class="rec-detail text-muted">{{ rec.detail }}</div>
          <div class="rec-date text-muted">By: {{ rec.by }}</div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const store = useAppStore()

interface Resource { name: string; pct: number; detail: string }
interface PredictionDay { date: string; label: string; cpu: number; memory: number; llmRate: number }
interface Recommendation { id: string; priority: 'high' | 'medium' | 'low'; title: string; detail: string; by: string }
interface Capacity { current: Resource[]; prediction: PredictionDay[]; recommendations: Recommendation[] }

const loading = ref(false)
const capacity = ref<Capacity>({ current: [], prediction: [], recommendations: [] })

const demoCapacity: Capacity = {
  current: [
    { name: 'CPU', pct: 62, detail: '6.2 / 10 cores avg' },
    { name: 'Memory', pct: 74, detail: '29.6 / 40 GB used' },
    { name: 'Queue Depth', pct: 38, detail: '1,140 / 3,000 jobs' },
    { name: 'LLM Rate Limit', pct: 88, detail: '264 / 300 RPM used' },
  ],
  prediction: [
    { date: '2026-03-22', label: 'Today', cpu: 62, memory: 74, llmRate: 88 },
    { date: '2026-03-23', label: 'Sun', cpu: 48, memory: 70, llmRate: 65 },
    { date: '2026-03-24', label: 'Mon', cpu: 75, memory: 78, llmRate: 92 },
    { date: '2026-03-25', label: 'Tue', cpu: 83, memory: 82, llmRate: 98 },
    { date: '2026-03-26', label: 'Wed', cpu: 79, memory: 80, llmRate: 95 },
    { date: '2026-03-27', label: 'Thu', cpu: 71, memory: 76, llmRate: 88 },
    { date: '2026-03-28', label: 'Fri', cpu: 68, memory: 74, llmRate: 82 },
  ],
  recommendations: [
    { id: 'r1', priority: 'high', title: 'Scale up LLM workers by 2 nodes', detail: 'LLM rate limit exceeds 80% threshold. Add 2 Groq or OpenAI router workers to prevent throttling.', by: '2026-03-24' },
    { id: 'r2', priority: 'high', title: 'Increase memory allocation for RAG engine', detail: 'Predicted memory usage peaks at 82% on Tue–Wed. Increase RAG service from 8GB to 12GB.', by: '2026-03-24' },
    { id: 'r3', priority: 'medium', title: 'Optimize CPU for PlannerAgent workers', detail: 'CPU spike expected Mon–Tue. Consider limiting max concurrent planner sessions to 15.', by: '2026-03-25' },
    { id: 'r4', priority: 'low', title: 'Archive old context traces', detail: 'Context traces table growing 2GB/week. Schedule weekly archive job.', by: '2026-03-28' },
  ]
}

function resourceValueClass(pct: number) {
  if (pct < 60) return 'text-ok'
  if (pct < 80) return 'text-warn'
  return 'text-danger'
}
function resourceBarClass(pct: number) {
  if (pct < 60) return 'rbar-ok'
  if (pct < 80) return 'rbar-warn'
  return 'rbar-err'
}
function priorityBadge(p: string) {
  if (p === 'high') return 'badge-err'
  if (p === 'medium') return 'badge-warn'
  return 'badge-info'
}

async function loadData() {
  loading.value = true
  try {
    const res = await api<any>('/capacity/predict')
    capacity.value = res
  } catch {
    capacity.value = demoCapacity
  } finally {
    loading.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.capacity-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.resource-card { flex: 1; min-width: 160px; padding: 1rem 1.25rem; }
.resource-name { font-size: .7rem; letter-spacing: .06em; margin-bottom: .3rem; }
.resource-value { font-size: 2.5rem; font-weight: 700; font-family: var(--font-mono); line-height: 1; margin-bottom: .3rem; }
.resource-bar-wrap { height: 5px; background: var(--bg3); border-radius: 3px; margin-bottom: .35rem; }
.resource-bar-fill { height: 100%; border-radius: 3px; transition: width .5s ease; }
.rbar-ok { background: var(--accent3); }
.rbar-warn { background: var(--warn); }
.rbar-err { background: var(--danger); }
.resource-detail { font-size: .75rem; }

.chart-title { font-weight: 700; font-size: .88rem; margin-bottom: .75rem; align-items: center; }
.legend-dot {
  width: 10px; height: 10px; border-radius: 2px; display: inline-block;
}
.cpu { background: var(--accent); }
.mem { background: var(--accent2); }
.llm { background: var(--accent3); }

.bar-chart {
  display: flex;
  gap: .35rem;
  height: 200px;
  align-items: flex-end;
}
.bar-y-labels {
  display: flex;
  flex-direction: column;
  justify-content: space-between;
  align-items: flex-end;
  font-size: .62rem;
  color: var(--text-muted);
  font-family: var(--font-mono);
  height: 160px;
  padding-right: .35rem;
  flex-shrink: 0;
}
.bar-area {
  display: flex;
  gap: .75rem;
  align-items: flex-end;
  flex: 1;
  height: 160px;
  border-bottom: 1px solid var(--panel-border);
  border-left: 1px solid var(--panel-border);
  padding: 0 .5rem;
}
.bar-day {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex: 1;
  height: 100%;
  justify-content: flex-end;
  gap: .2rem;
}
.stacked-bars {
  display: flex;
  align-items: flex-end;
  width: 14px;
  height: 100%;
}
.bar-seg {
  width: 100%;
  border-radius: 2px 2px 0 0;
  transition: height .4s ease;
}
.cpu-seg { background: var(--accent); }
.mem-seg { background: var(--accent2); }
.llm-seg { background: var(--accent3); }
.day-label { font-size: .65rem; color: var(--text-muted); font-family: var(--font-mono); margin-top: .2rem; }
.threshold-line-label { font-size: .7rem; margin-top: .5rem; }

.section-title { font-weight: 700; font-size: .9rem; padding: .75rem 1rem; border-bottom: 1px solid var(--panel-border); }
.recs-list { display: flex; flex-direction: column; gap: 0; }
.rec-item {
  padding: .9rem 1rem;
  border-bottom: 1px solid var(--panel-border);
  border-left: 3px solid transparent;
  transition: var(--transition);
}
.rec-item:last-child { border-bottom: none; }
.rec-high { border-left-color: var(--danger); }
.rec-medium { border-left-color: var(--warn); }
.rec-low { border-left-color: var(--text-muted); }
.rec-title { font-weight: 600; font-size: .9rem; }
.rec-detail { font-size: .82rem; margin: .3rem 0 .2rem; }
.rec-date { font-size: .75rem; }
</style>
