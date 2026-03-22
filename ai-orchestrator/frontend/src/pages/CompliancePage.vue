<template>
  <div class="compliance-page">
    <div class="page-header flex items-center justify-between">
      <div>
        <h1 class="page-title">Compliance Report</h1>
        <p class="text-muted">Governance, regulatory controls, and tenant isolation status</p>
      </div>
      <div class="flex gap-2">
        <button class="btn btn-ghost" @click="exportPdf" :disabled="exporting">
          {{ exporting ? 'Exporting...' : 'Export PDF' }}
        </button>
        <button class="btn btn-primary" @click="generateReport" :disabled="generating">
          {{ generating ? 'Generating...' : 'Generate Report' }}
        </button>
      </div>
    </div>

    <!-- Score Gauge -->
    <div class="flex gap-2 items-start" style="margin-bottom:1.5rem; flex-wrap:wrap;">
      <div class="card score-card">
        <div class="text-muted" style="font-size:.75rem; letter-spacing:.06em;">COMPLIANCE SCORE</div>
        <div class="score-display" :class="scoreClass">{{ report.score }}</div>
        <div class="score-label text-muted">{{ scoreLabel }}</div>
        <div class="score-bar-wrap">
          <div class="score-bar-fill" :class="scoreBarClass" :style="{ width: report.score + '%' }"></div>
        </div>
        <div class="text-muted" style="font-size:.7rem; margin-top:.5rem;">Last checked: {{ report.lastChecked }}</div>
      </div>

      <div class="card kpi-mini">
        <div class="kpi-mini-label text-muted">PASSED</div>
        <div class="kpi-mini-value text-ok">{{ passCount }}</div>
      </div>
      <div class="card kpi-mini">
        <div class="kpi-mini-label text-muted">WARNINGS</div>
        <div class="kpi-mini-value text-warn">{{ warnCount }}</div>
      </div>
      <div class="card kpi-mini">
        <div class="kpi-mini-label text-muted">FAILED</div>
        <div class="kpi-mini-value text-danger">{{ failCount }}</div>
      </div>
    </div>

    <!-- Controls Checklist -->
    <div class="card" style="margin-bottom:1rem;">
      <div class="section-title">Compliance Controls</div>
      <table class="tbl">
        <thead>
          <tr>
            <th>Control</th>
            <th>Framework</th>
            <th>Status</th>
            <th>Last Checked</th>
            <th>Description</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="ctrl in report.controls" :key="ctrl.id">
            <td style="font-weight:600; font-size:.88rem;">{{ ctrl.name }}</td>
            <td><span class="badge badge-info">{{ ctrl.framework }}</span></td>
            <td>
              <span class="badge" :class="controlStatusBadge(ctrl.status)">{{ ctrl.status }}</span>
            </td>
            <td class="text-muted" style="font-size:.78rem;">{{ ctrl.lastChecked }}</td>
            <td class="text-muted" style="font-size:.8rem;">{{ ctrl.description }}</td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Tenant Isolation Scan -->
    <div class="card">
      <div class="section-title flex items-center justify-between">
        <span>Tenant Isolation Scan</span>
        <span class="badge" :class="report.isolationScan.status === 'clean' ? 'badge-ok' : 'badge-err'">
          {{ report.isolationScan.status }}
        </span>
      </div>
      <div class="isolation-details">
        <div class="isolation-stat">
          <span class="text-muted" style="font-size:.78rem;">Tenants Scanned</span>
          <span class="mono text-accent">{{ report.isolationScan.tenantsScanned }}</span>
        </div>
        <div class="isolation-stat">
          <span class="text-muted" style="font-size:.78rem;">Cross-tenant Violations</span>
          <span class="mono" :class="report.isolationScan.violations === 0 ? 'text-ok' : 'text-danger'">
            {{ report.isolationScan.violations }}
          </span>
        </div>
        <div class="isolation-stat">
          <span class="text-muted" style="font-size:.78rem;">Scan Duration</span>
          <span class="mono text-muted">{{ report.isolationScan.duration }}</span>
        </div>
        <div class="isolation-stat">
          <span class="text-muted" style="font-size:.78rem;">Last Run</span>
          <span class="text-muted" style="font-size:.85rem;">{{ report.isolationScan.lastRun }}</span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useApi } from '@/composables/useApi'
import { useAppStore } from '@/stores/app'

const { api } = useApi()
const store = useAppStore()

interface Control {
  id: string
  name: string
  framework: string
  status: 'pass' | 'fail' | 'warning'
  lastChecked: string
  description: string
}
interface Report {
  score: number
  lastChecked: string
  controls: Control[]
  isolationScan: { status: string; tenantsScanned: number; violations: number; duration: string; lastRun: string }
}

const generating = ref(false)
const exporting = ref(false)
const report = ref<Report>({
  score: 87,
  lastChecked: '2026-03-22 14:00',
  controls: [],
  isolationScan: { status: 'clean', tenantsScanned: 5, violations: 0, duration: '2.3s', lastRun: '2026-03-22 13:55' }
})

const demoReport: Report = {
  score: 87,
  lastChecked: '2026-03-22 14:00',
  controls: [
    { id: 'c1', name: 'Data Encryption at Rest', framework: 'GDPR', status: 'pass', lastChecked: '2026-03-22 14:00', description: 'All PII fields encrypted with AES-256' },
    { id: 'c2', name: 'Data Encryption in Transit', framework: 'SOC2', status: 'pass', lastChecked: '2026-03-22 14:00', description: 'TLS 1.3 enforced on all endpoints' },
    { id: 'c3', name: 'Access Control Audit', framework: 'SOC2', status: 'pass', lastChecked: '2026-03-22 14:00', description: 'RBAC enforced, audit logs enabled' },
    { id: 'c4', name: 'Right to Erasure', framework: 'GDPR', status: 'warning', lastChecked: '2026-03-22 14:00', description: 'Deletion pipeline exists but avg 48h delay noted' },
    { id: 'c5', name: 'Data Minimization', framework: 'GDPR', status: 'pass', lastChecked: '2026-03-22 14:00', description: 'Only necessary fields collected per data schema' },
    { id: 'c6', name: 'PHI Isolation', framework: 'HIPAA', status: 'warning', lastChecked: '2026-03-22 14:00', description: 'PHI handling path not yet HIPAA-certified' },
    { id: 'c7', name: 'Vulnerability Scanning', framework: 'SOC2', status: 'pass', lastChecked: '2026-03-21 00:00', description: 'Weekly SAST/DAST scans, no critical CVEs' },
    { id: 'c8', name: 'Incident Response Plan', framework: 'SOC2', status: 'fail', lastChecked: '2026-03-15 00:00', description: 'IRP documented but tabletop exercise overdue' },
    { id: 'c9', name: 'Data Retention Policy', framework: 'GDPR', status: 'pass', lastChecked: '2026-03-22 14:00', description: 'Automated expiry enforced per retention schedule' },
    { id: 'c10', name: 'Breach Notification Procedure', framework: 'GDPR', status: 'pass', lastChecked: '2026-03-22 14:00', description: '72-hour notification SOP documented and tested' },
  ],
  isolationScan: { status: 'clean', tenantsScanned: 5, violations: 0, duration: '2.3s', lastRun: '2026-03-22 13:55' }
}

const passCount = computed(() => report.value.controls.filter(c => c.status === 'pass').length)
const warnCount = computed(() => report.value.controls.filter(c => c.status === 'warning').length)
const failCount = computed(() => report.value.controls.filter(c => c.status === 'fail').length)

const scoreClass = computed(() => {
  const s = report.value.score
  if (s >= 85) return 'score-ok'
  if (s >= 65) return 'score-warn'
  return 'score-err'
})
const scoreBarClass = computed(() => {
  const s = report.value.score
  if (s >= 85) return 'sbar-ok'
  if (s >= 65) return 'sbar-warn'
  return 'sbar-err'
})
const scoreLabel = computed(() => {
  const s = report.value.score
  if (s >= 85) return 'Compliant'
  if (s >= 65) return 'Needs Attention'
  return 'Non-Compliant'
})

function controlStatusBadge(status: string) {
  if (status === 'pass') return 'badge-ok'
  if (status === 'warning') return 'badge-warn'
  return 'badge-err'
}

async function generateReport() {
  generating.value = true
  try {
    const res = await api<any>('/compliance/report')
    report.value = res
    store.showToast('Report generated', 'ok')
  } catch {
    report.value = demoReport
    store.showToast('Using demo data', 'info')
  } finally {
    generating.value = false
  }
}

async function exportPdf() {
  exporting.value = true
  try {
    await api('/compliance/export-pdf', { method: 'POST' })
    store.showToast('PDF export queued', 'ok')
  } catch {
    store.showToast('Export failed', 'err')
  } finally {
    exporting.value = false
  }
}

async function loadData() {
  try {
    const res = await api<any>('/compliance/report')
    report.value = {
      ...demoReport,
      ...res,
      controls: Array.isArray(res.controls) ? res.controls : demoReport.controls,
      isolationScan: res.isolationScan ?? demoReport.isolationScan,
    }
  } catch {
    report.value = demoReport
  }
}

onMounted(loadData)
</script>

<style scoped>
.compliance-page { padding: 1.5rem; max-width: 1400px; }
.page-header { margin-bottom: 1.5rem; }
.page-title { font-size: 1.5rem; font-weight: 700; margin: 0 0 .25rem; }

.score-card { padding: 1.25rem 1.5rem; min-width: 200px; }
.score-display {
  font-size: 4rem;
  font-weight: 900;
  font-family: var(--font-mono);
  line-height: 1;
  margin: .5rem 0 .25rem;
}
.score-ok { color: var(--accent3); }
.score-warn { color: var(--warn); }
.score-err { color: var(--danger); }
.score-label { font-size: .85rem; margin-bottom: .5rem; }
.score-bar-wrap { height: 6px; background: var(--bg3); border-radius: 3px; }
.score-bar-fill { height: 100%; border-radius: 3px; transition: width .6s ease; }
.sbar-ok { background: var(--accent3); }
.sbar-warn { background: var(--warn); }
.sbar-err { background: var(--danger); }

.kpi-mini { padding: .75rem 1.25rem; min-width: 110px; display: flex; flex-direction: column; gap: .25rem; }
.kpi-mini-label { font-size: .7rem; letter-spacing: .06em; }
.kpi-mini-value { font-size: 2.5rem; font-weight: 700; font-family: var(--font-mono); line-height: 1; }

.section-title { font-weight: 700; font-size: .9rem; padding: .75rem 1rem; border-bottom: 1px solid var(--panel-border); }

.isolation-details {
  display: flex;
  gap: 2rem;
  padding: 1rem;
  flex-wrap: wrap;
}
.isolation-stat {
  display: flex;
  flex-direction: column;
  gap: .25rem;
}
.isolation-stat .mono { font-size: 1.2rem; font-weight: 700; }
</style>
