/**
 * dashboard.js
 * ============
 * Professional Dynamic Logic for SINC AI Dashboard.
 * Extracted from monolithic dashboard.py.
 */

const AK_PARAM = new URLSearchParams(location.search).get('api_key');
let AK = AK_PARAM || localStorage.getItem('sinc_dashboard_api_key') || '';
const TN = 'FERNANDO_P0';
let selectedTaskId = null;
let selectedTaskDebugger = null;
let selectedDebuggerTab = 'metadata';
let pipelineCache = [];
let feedPaused = false;
let liveFeedCache = [];
let feedCache = [];
let feedFilter = 'all';
let feedExplorerSearch = '';
let feedExplorerAgent = '';
let feedExplorerTaskId = '';
let feedExplorerWindowHours = 24;
let feedExplorerSnapshotTs = '';
let feedExplorerOffset = 0;
let feedExplorerLimit = 8;
let feedExplorerHasMore = false;
let feedExplorerActive = false;
let runtimeConfig = { confidence: 72, mode: 'normal' };
let infraCache = null;
let authInvalid = false;
let authFailureNotified = false;

function showView(name) {
    document.getElementById('v-noc').style.display = name === 'noc' ? 'flex' : 'none';
    document.getElementById('v-diagnostics').style.display = name === 'diagnostics' ? 'flex' : 'none';
    if (name === 'diagnostics') {
        loadDiagnostics();
        loadDiagLogs('worker');
    }
}

function stopRealtime() {
    if (syncHandle !== null) {
        clearInterval(syncHandle);
        syncHandle = null;
    }
    if (es) {
        es.close();
        es = null;
    }
}

function handleAuthFailure(statusCode, path) {
    stopRealtime();
    authInvalid = true;
    if (AK) {
        localStorage.removeItem('sinc_dashboard_api_key');
    }
    AK = '';
    if (!authFailureNotified) {
        authFailureNotified = true;
        showToast(`Authentication expired for ${path} (${statusCode}). Provide a valid API key to resume.`);
    }
}

async function apiJson(path, options = {}) {
    const headers = new Headers(options.headers || {});
    if (AK && !headers.has('X-Api-Key') && !String(path).includes('api_key=')) {
        headers.set('X-Api-Key', AK);
    }
    const res = await fetch(path, { ...options, headers });
    if (res.status === 401 || res.status === 403) {
        handleAuthFailure(res.status, path);
        throw new Error(`auth:${res.status}:${path}`);
    }
    if (!res.ok) {
        throw new Error(`http:${res.status}:${path}`);
    }
    return await res.json();
}

/** 
 * UI Rendering Helpers 
 */
function escapeHtml(value) {
    return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#39;');
}

function fmtBytes(value) {
    const num = Number(value || 0);
    if (!num) return 'n/a';
    const gb = num / (1024 ** 3);
    if (gb >= 1) return `${gb.toFixed(1)} GB`;
    const mb = num / (1024 ** 2);
    return `${mb.toFixed(0)} MB`;
}

function pct(value, fallback = 0) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return fallback;
    return Math.max(0, Math.min(100, Number(value)));
}

/**
 * Real-time Synchronizers
 */
async function sync() {
    if (!AK || authInvalid) return;
    try {
        const d = await apiJson('/api/v5/dashboard/summary');
        // ... (Logic extracted from dashboard.py sink function)
        // I will implement a cleaner, summarized version here to keep it Elite.
        updateDashboardKPIs(d.metrics);
        renderAgentFleet(d.agent_fleet);
        renderPipeline(d.pipeline || []);
        // ...
    } catch (e) {
        console.error("Sync Failure", e);
    }
}

function updateDashboardKPIs(m) {
    if (!m) return;
    document.getElementById('k-success').textContent = Math.round(m.success_rate * 100) + '%';
    document.getElementById('k-auton').textContent = m.autonomy_score.toFixed(2);
    document.getElementById('k-latency').textContent = (m.latency_p95 || 0) + 'ms';
    document.getElementById('k-recovery').textContent = Math.round(m.recovery_rate * 100) + '%';
}

function showToast(m) {
    const t = document.getElementById('toast');
    if (t) {
        t.textContent = m;
        t.classList.add('show');
        setTimeout(() => t.classList.remove('show'), 3000);
    }
}

/**
 * Initialization
 */
let syncHandle = null;
let es = null;

function startRealtime() {
    // Check API Key, start interval, setup EventSource
    console.log("SINC AI Dashboard: Initializing Real-time Pipeline...");
    sync();
    syncHandle = setInterval(sync, 4000);
}

// ... (Rest of JS logic will be ported here in a clean, modular way)
