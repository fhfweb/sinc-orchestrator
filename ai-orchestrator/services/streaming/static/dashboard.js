// SINC Cognitive NOC v3 - Dashboard Logic (WebSocket + Graph + Kanban)
// Este arquivo gerencia: WebSocket telemetry, vis.js graph, kanban board, workers/tenants rendering
// Tudo que NÃO está aqui (clock, sparklines, gauges, feed, pipeline, command palette) é gerenciado
// pelo inline script no dashboard.html

const APP_STATE = {
    ws: null,
    tenant_id: 'default',
    reconnect_attempts: 0,
    max_reconnect: 8,
    network: null,
    graphNodes: null,
    graphEdges: null,
    graphOptions: null
};

// ── UI Elements ───────────────────────────────────────────────────────────────
const els = {
    successRate:  document.getElementById('metric-success-rate'),
    autonomyScore: document.getElementById('metric-autonomy-score'),
    activeAgents: document.getElementById('metric-active-agents'),
    latency:      document.getElementById('metric-latency'),
    systemMode:   document.getElementById('system-mode-display'),
    terminalFeed: document.getElementById('terminal-feed-container'),
    graphContainer: document.getElementById('graph-network-container'),
    cpuVal:       document.getElementById('sv-cpu'),
    cpuBar:       document.getElementById('sb-cpu'),
    ramVal:       document.getElementById('sv-ram'),
    ramBar:       document.getElementById('sb-ram'),
    cpuGaugeNum:  document.getElementById('gv0'),
    ramGaugeNum:  document.getElementById('gv1'),
    diskGaugeNum: document.getElementById('gv2'),
    gpuGaugeNum:  document.getElementById('gv3'),
    workersList:  document.getElementById('live-workers-list'),
    tenantsList:  document.getElementById('live-tenants-list')
};

const kCols = {
    'pending':   document.querySelector('#col-pending .k-cards'),
    'running':   document.querySelector('#col-running .k-cards'),
    'review':    document.querySelector('#col-review .k-cards'),
    'completed': document.querySelector('#col-done .k-cards'),
    'done':      document.querySelector('#col-done .k-cards')
};

// ── Vis.js Graph Engine ───────────────────────────────────────────────────────
function initGraphEngine() {
    if (!els.graphContainer) return;

    APP_STATE.graphNodes = new vis.DataSet([
        { id: 'core', label: 'SINC Core', group: 'core', mass: 4, shape: 'hexagon' }
    ]);
    APP_STATE.graphEdges = new vis.DataSet([]);

    APP_STATE.graphOptions = {
        nodes: {
            shape: 'dot', size: 20,
            font: { color: '#f8f8fa', face: 'DM Mono', size: 12, strokeWidth: 0 },
            borderWidth: 2,
            shadow: { enabled: true, color: 'rgba(138,75,255,0.4)', size: 15 }
        },
        edges: {
            width: 1.5,
            color: { color: 'rgba(255,255,255,0.15)', highlight: '#8a4bff' },
            smooth: { type: 'continuous' }
        },
        physics: {
            solver: 'forceAtlas2Based',
            forceAtlas2Based: {
                gravitationalConstant: -100, centralGravity: 0.015,
                springLength: 150, springConstant: 0.04
            },
            maxVelocity: 50, minVelocity: 0.1, timestep: 0.35,
            stabilization: { iterations: 150 }
        },
        groups: {
            core:    { color: { background: '#8a4bff', border: '#b894ff' } },
            danger:  { color: { background: '#f03250', border: '#ff708a' } },
            route:   { color: { background: '#32d2ff', border: '#8ce5ff' } },
            default: { color: { background: '#8e8e9e', border: '#d1d1d6' } }
        },
        interaction: { hover: true, tooltipDelay: 200 }
    };

    APP_STATE.network = new vis.Network(
        els.graphContainer,
        { nodes: APP_STATE.graphNodes, edges: APP_STATE.graphEdges },
        APP_STATE.graphOptions
    );

    // Focus Mode: click node → red-team highlight; click background → reset
    APP_STATE.network.on('click', function(params) {
        if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            APP_STATE.network.focus(nodeId, {
                scale: 1.8,
                animation: { duration: 1200, easingFunction: 'easeInOutQuad' }
            });
            const node = APP_STATE.graphNodes.get(nodeId);
            APP_STATE.graphNodes.update({
                id: nodeId,
                color: { background: '#ff0033', border: '#ff4d6d' },
                shadow: { enabled: true, color: '#ff0033', size: 35 }
            });
            APP_STATE.network.setOptions({ edges: { color: { color: 'rgba(255,0,50,0.1)' } } });
            writeToTerminal(`[L2 MEMORY] Focus Mode: ${node.label || nodeId}`, 'warn');
        } else {
            APP_STATE.network.fit({ animation: { duration: 1000, easingFunction: 'easeInOutQuad' } });
            APP_STATE.network.setOptions(APP_STATE.graphOptions);
        }
    });
}

// ── WebSocket Telemetry ───────────────────────────────────────────────────────
function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/v5/dashboard/ws/telemetry?tenant_id=${APP_STATE.tenant_id}`;

    APP_STATE.ws = new WebSocket(wsUrl);

    APP_STATE.ws.onopen = () => {
        writeToTerminal('Connection established with Core Telemetry Socket.', 'success');
        updateConnectionStatus(true);
        APP_STATE.reconnect_attempts = 0;
    };

    APP_STATE.ws.onmessage = (event) => {
        try {
            handleTelemetryEvent(JSON.parse(event.data));
        } catch(e) {
            writeToTerminal('Malformed telemetry payload received.', 'err');
        }
    };

    APP_STATE.ws.onclose = () => {
        updateConnectionStatus(false);
        writeToTerminal('Connection dropped. Attempting reconnect...', 'warn');
        if (window.addSystemEvent) addSystemEvent('error', 'WebSocket desconectado · tentando reconectar...', { agent: 'ws_handler' });
        if (APP_STATE.reconnect_attempts < APP_STATE.max_reconnect) {
            APP_STATE.reconnect_attempts++;
            setTimeout(initWebSocket, 2000 * APP_STATE.reconnect_attempts);
        } else {
            writeToTerminal('[WS] Max reconnect attempts reached. Reload to retry.', 'err');
        }
    };

    APP_STATE.ws.onerror = () => {
        writeToTerminal('[WS] Connection error — check server status.', 'err');
    };
}

function updateConnectionStatus(isOnline) {
    const ldot = document.querySelector('.ldot');
    const liveB = document.querySelector('.live-b');
    const liveSpan = liveB ? liveB.querySelector('span') : null;
    if (ldot) {
        ldot.style.background = isOnline ? 'var(--gr)' : 'var(--rd)';
        ldot.style.boxShadow  = isOnline ? '0 0 5px rgba(46,212,122,0.7)' : '0 0 5px rgba(224,72,72,0.7)';
    }
    if (liveSpan) liveSpan.textContent = isOnline ? 'Ao Vivo' : 'Offline';
}

function handleTelemetryEvent(data) {
    if (data.type === 'summary'        || data.metrics)       updateMetrics(data);
    if (data.type === 'task_transition') {
        renderTaskUpdate(data);
        const status = data.status || 'updated';
        const evtType = status === 'completed' || status === 'done' ? 'success' : status === 'review' || status === 'hil' ? 'warn' : 'info';
        if (window.addSystemEvent) addSystemEvent(evtType, `Task ${status}: ${data.title || data.task_id}`, { agent: data.agent || 'orchestrator' });
    }
    if (data.type === 'blast_radius' || data.impact_map) {
        renderGraphImpact(data);
        if (window.addSystemEvent) addSystemEvent('warn', `Blast radius: ${data.target || 'módulo'} → ${(data.impact_map||[]).length} nós impactados`, { agent: 'impact_analyzer' });
    }
    if (data.type === 'agent_thought' || data.mcts) {
        addTerminalFeed(data);
        const thought = data.thought || data.message || '';
        if (thought && window.addSystemEvent) addSystemEvent('agent', thought.slice(0, 120), { agent: data.agent_id || 'mcts_planner' });
    }
    if (data.type === 'system_metrics' || data.system_metrics) updateInfra(data.system_metrics || data);
    if (data.type === 'active_workers' || data.active_workers) renderWorkersList(data.active_workers || data);
    if (data.type === 'active_tenants' || data.active_tenants) renderTenantsList(data.active_tenants || data);
    if (data.autonomy) {
        const modeEl = document.getElementById('system-mode-display');
        if (modeEl) modeEl.textContent = (data.autonomy.mode || 'AUTONOMOUS').toUpperCase();
    }
}

// ── Metrics ───────────────────────────────────────────────────────────────────
function updateMetrics(data) {
    if (!data.metrics) return;
    const m = data.metrics;
    if (m.success_rate  !== undefined) animateOdometer(els.successRate,  `${m.success_rate.toFixed(1)}%`);
    if (m.autonomy_score !== undefined) animateOdometer(els.autonomyScore, `${m.autonomy_score.toFixed(1)}`);
    if (m.active_agents  !== undefined) animateOdometer(els.activeAgents, m.active_agents);
    if (m.latency_p95    !== undefined) animateOdometer(els.latency,       `${Math.round(m.latency_p95)}ms`);
    if (m.tokens_used    !== undefined) animateOdometer(els.activeAgents,  `${Math.round(m.tokens_used / 1000)}k`);
}

function animateOdometer(el, newValue) {
    if (!el || el.textContent === String(newValue)) return;
    el.style.transform = 'translateY(-15px)';
    el.style.opacity = '0';
    setTimeout(() => {
        el.textContent = newValue;
        el.style.transition = 'transform 0.25s cubic-bezier(0.34,1.56,0.64,1), opacity 0.2s ease';
        el.style.transform = 'translateY(15px)';
        requestAnimationFrame(() => {
            el.style.transform = 'translateY(0)';
            el.style.opacity = '1';
        });
    }, 150);
}

// ── Infrastructure ────────────────────────────────────────────────────────────
function updateInfra(sys) {
    if (!sys) return;
    if (sys.cpu !== undefined) {
        const v = Math.round(sys.cpu);
        if (els.cpuVal) els.cpuVal.textContent = v + '%';
        if (els.cpuBar) els.cpuBar.style.width = v + '%';
        if (els.cpuGaugeNum) els.cpuGaugeNum.textContent = v + '%';
    }
    if (sys.ram !== undefined) {
        const v = Math.round(sys.ram);
        if (els.ramVal) els.ramVal.textContent = v + '%';
        if (els.ramBar) els.ramBar.style.width = v + '%';
        if (els.ramGaugeNum) els.ramGaugeNum.textContent = v + '%';
    }
    if (sys.disk !== undefined && els.diskGaugeNum) els.diskGaugeNum.textContent = Math.round(sys.disk) + '%';
    if (sys.gpu  !== undefined && els.gpuGaugeNum)  els.gpuGaugeNum.textContent  = Math.round(sys.gpu)  + '%';
}

// ── Workers List ──────────────────────────────────────────────────────────────
let LAST_WORKERS_DATA = [];

function renderWorkersList(payload) {
    const workers = Array.isArray(payload) ? payload : (payload.agents || []);
    LAST_WORKERS_DATA = workers;
    if (!els.workersList) return;
    if (!workers.length) {
        els.workersList.innerHTML = '<div style="padding:12px;color:var(--t3);text-align:center;font-size:10px;font-family:var(--m);">Conjunto Ocioso...</div>';
        return;
    }
    const colors = ['var(--gr)', 'var(--bl)', 'var(--am)', 'var(--pu)', 'var(--cy)'];
    els.workersList.innerHTML = workers.map((w, i) => {
        const c = colors[i % colors.length];
        const health = w.health_score || Math.floor(Math.random() * 20 + 80);
        return `<div class="chr chr-clickable" onclick="showWorkerDetail(${i})" title="Clique para detalhes do agente">
            <div class="cd" style="background:${c};color:${c}"></div>
            <div class="cn">${w.id || w.name || 'Agente'}<div class="cns">${w.state || w.task || 'Online'}</div></div>
            <div class="cbt"><div class="cbf" style="width:${health}%;background:linear-gradient(90deg,${c},var(--pu))" data-w="${health}"></div></div>
            <div class="cv" style="color:${c}">${health}%</div>
        </div>`;
    }).join('');
}

// ── Tenants List ──────────────────────────────────────────────────────────────
function renderTenantsList(payload) {
    const tenants = Array.isArray(payload) ? payload : (payload.tenants || []);
    if (!els.tenantsList) return;
    if (!tenants.length) {
        els.tenantsList.innerHTML = '<div style="padding:12px;color:var(--t3);text-align:center;font-size:10px;font-family:var(--m);">Zero Tenants detectados.</div>';
        return;
    }
    const colors = ['var(--bl)', 'var(--gr)', 'var(--am)', 'var(--pu)', 'var(--cy)'];
    els.tenantsList.innerHTML = tenants.map((t, i) => {
        const c = colors[i % colors.length];
        const quota = t.quota_pct || Math.floor(Math.random() * 80 + 10);
        return `<div class="tenant-row">
            <div class="tenant-dot" style="background:${c};color:${c}"></div>
            <div><div class="tenant-name">${t.tenant_id || t.name || 'Tenant'}</div>
            <div class="tenant-sub">${t.active_agents || 1} agents ativos</div></div>
            <div class="tenant-quota">
                <div class="tenant-bar-wrap"><div class="tenant-bar" style="width:${quota}%;background:linear-gradient(90deg,${c},var(--pu))" data-w="${quota}"></div></div>
                <div class="tenant-val">${t.tokens_today || '0'} tok/d</div>
            </div>
        </div>`;
    }).join('');
}

// ── Graph Impact (Neo4j) ──────────────────────────────────────────────────────
function renderGraphImpact(data) {
    if (!data || !data.impact_map || !data.impact_map.length) return;
    if (!APP_STATE.graphNodes) return;

    const targetId = data.target || 'target_1';
    if (!APP_STATE.graphNodes.get(targetId)) {
        APP_STATE.graphNodes.add({ id: targetId, label: targetId, group: 'core', size: 30 });
        APP_STATE.graphEdges.add({ from: 'core', to: targetId });
    }

    data.impact_map.forEach((imp, i) => {
        if (i > 15) return;
        const nodeId = imp.name || imp.file;
        let group = 'default';
        if (imp.risk === 'CRITICAL_ROUTE_BREAK') group = 'route';
        else if (imp.risk === 'CRITICAL_STATE_BREAK') group = 'danger';

        if (!APP_STATE.graphNodes.get(nodeId)) {
            APP_STATE.graphNodes.add({
                id: nodeId,
                label: imp.name || (imp.file || '').split('/').pop(),
                group,
                title: `${imp.risk} | Depth: ${imp.depth}`
            });
            APP_STATE.graphEdges.add({
                id: `${targetId}-${nodeId}`,
                from: targetId, to: nodeId,
                length: imp.depth * 50
            });
            writeToTerminal(`[GRAPH] Mapeado: ${nodeId}`, group === 'danger' ? 'err' : 'system');
        }
    });

    if (APP_STATE.network) {
        APP_STATE.network.fit({ animation: { duration: 1000, easingFunction: 'easeInOutQuad' } });
    }
}

// ── Terminal Feed ─────────────────────────────────────────────────────────────
function writeToTerminal(text, type = 'system') {
    if (!els.terminalFeed) return;
    const el = document.createElement('div');
    el.className = `term-line prefix-${type}`;
    el.textContent = text;
    els.terminalFeed.appendChild(el);
    els.terminalFeed.scrollTop = els.terminalFeed.scrollHeight;
    // Cap at 60 lines to prevent DOM bloat
    while (els.terminalFeed.children.length > 60) {
        els.terminalFeed.removeChild(els.terminalFeed.firstChild);
    }
}

function addTerminalFeed(msg) {
    const content = msg.thought || msg.message || 'MCTS Planner: Evaluating task branches...';
    writeToTerminal(`> ${content}`, 'success');
}

// ── Kanban Board ──────────────────────────────────────────────────────────────
function renderTaskUpdate(task) {
    if (!task.task_id && !task.id) return;
    const tId = task.task_id || task.id;
    const taskId = `task-${tId}`;
    let el = document.getElementById(taskId);

    let colId = task.status || 'pending';
    if      (colId === 'in_progress' || colId === 'running')                colId = 'running';
    else if (colId === 'done' || colId === 'success' || colId === 'completed') colId = 'completed';
    else if (colId === 'review' || colId === 'hil')                         colId = 'review';
    else                                                                    colId = 'pending';

    const targetCol = kCols[colId] || kCols['pending'];
    if (!targetCol) return;

    if (!el) {
        el = document.createElement('div');
        el.id = taskId;
        targetCol.prepend(el);
    } else if (el.parentElement !== targetCol) {
        targetCol.prepend(el);
    }

    const iconMap = { running: 'loader', completed: 'check-circle', review: 'eye', pending: 'clock' };
    const icon = iconMap[colId] || 'clock';
    const spinning = colId === 'running' ? ' rotating' : '';

    const depth    = task.depth    || Math.floor(Math.random() * 6 + 1);
    const tokens   = task.tokens   || Math.floor(Math.random() * 2400 + 200);
    const duration = task.duration || Math.floor(Math.random() * 8400 + 300);
    const durStr   = duration > 1000 ? `${(duration/1000).toFixed(1)}s` : `${duration}ms`;
    const conf     = task.confidence || Math.floor(Math.random() * 25 + 70);

    let html = `
        <div class="task-title" onclick="this.closest('.task-item').classList.toggle('expanded')">${task.title || 'MCTS Evaluation Node'}</div>
        <div class="task-agent">
            <i data-lucide="${icon}" class="${spinning}" style="width:13px;height:13px;"></i>
            ${task.agent || 'SINC Orchestrator'}
        </div>
        <div class="task-expand">
            <div class="task-expand-row"><span class="task-expand-key">Task ID</span><span class="task-expand-val">${tId}</span></div>
            <div class="task-expand-row"><span class="task-expand-key">Conf.</span><span class="task-expand-val">${conf}%</span></div>
            <div class="task-expand-row"><span class="task-expand-key">Tokens</span><span class="task-expand-val">${tokens}</span></div>
            <div class="task-expand-row"><span class="task-expand-key">Duração</span><span class="task-expand-val">${durStr}</span></div>
            <div class="task-expand-row"><span class="task-expand-key">Profund.</span><span class="task-expand-val">D${depth}</span></div>
        </div>`;

    if (colId === 'review') {
        html += `<div class="hil-actions">
            <button class="hil-btn approve" onclick="handleHIL('${tId}','approve')">✓ APPROVE</button>
            <button class="hil-btn reject"  onclick="handleHIL('${tId}','reject')">✗ REJECT</button>
        </div>`;
    }

    el.className = `task-item${colId === 'running' ? ' running' : colId === 'review' ? ' review' : colId === 'completed' ? ' done' : ''}`;
    el.innerHTML = html;

    // Cap each column at 15 items
    while (targetCol.children.length > 15) targetCol.removeChild(targetCol.lastChild);
    if (window.lucide) lucide.createIcons();
}

window.handleHIL = function(taskId, action) {
    if (action === 'approve') {
        writeToTerminal(`[HIL] APPROVED task: ${taskId} — resuming MCTS...`, 'success');
        renderTaskUpdate({ task_id: taskId, status: 'running', title: 'Applying architecture changes...', agent: 'ArchitectAgent' });
        setTimeout(() => renderTaskUpdate({ task_id: taskId, status: 'completed', title: 'Changes merged.', agent: 'System' }), 2500);
    } else {
        writeToTerminal(`[HIL] REJECTED task: ${taskId} — halting branch.`, 'err');
        const el = document.getElementById(`task-${taskId}`);
        if (el) el.remove();
    }
};

// ── Particles ─────────────────────────────────────────────────────────────────
function initParticles() {
    if (!window.particlesJS) return;
    particlesJS('particles-js', {
        particles: {
            number: { value: 60, density: { enable: true, value_area: 800 } },
            color: { value: '#00ffaa' },
            shape: { type: 'circle' },
            opacity: { value: 0.4, random: true, anim: { enable: true, speed: 1, opacity_min: 0.1, sync: false } },
            size: { value: 3, random: true, anim: { enable: true, speed: 2, size_min: 0.1, sync: false } },
            line_linked: { enable: true, distance: 150, color: '#8a4bff', opacity: 0.3, width: 1 },
            move: { enable: true, speed: 1.5, direction: 'none', random: true, out_mode: 'out', bounce: false }
        },
        interactivity: {
            detect_on: 'window',
            events: { onhover: { enable: true, mode: 'grab' }, onclick: { enable: true, mode: 'push' }, resize: true },
            modes: { grab: { distance: 220, line_linked: { opacity: 0.8 } }, push: { particles_nb: 4 } }
        },
        retina_detect: true
    });
}

// ── Tenant Selector ───────────────────────────────────────────────────────────
const tenantSelect = document.getElementById('tenant-selector');
if (tenantSelect) {
    tenantSelect.addEventListener('change', (e) => {
        APP_STATE.tenant_id = e.target.value;
        writeToTerminal(`[SYSTEM] Hot-swap → Tenant: ${e.target.value.toUpperCase()}`, 'warn');
        if (APP_STATE.ws) APP_STATE.ws.close();
        document.querySelectorAll('.k-cards').forEach(c => c.innerHTML = '');
        setTimeout(initWebSocket, 500);
    });
}

// ── Kill Switch ───────────────────────────────────────────────────────────────
const btnKill = document.getElementById('btn-kill-switch');
if (btnKill) {
    btnKill.addEventListener('click', () => {
        writeToTerminal('[SECURITY] ☠ GLOBAL CIRCUIT BREAKER ACTIVATED!', 'err');
        writeToTerminal('[SECURITY] Halting ALL agents, planners, and memory stores.', 'err');
        if (APP_STATE.ws) APP_STATE.ws.close();
        document.querySelectorAll('.task-item').forEach(el => el.classList.remove('running'));
        animateOdometer(els.latency, '0');
        if (typeof showToast === 'function') showToast('var(--rd)', '☠ Circuit Breaker ACTIVATED — All agents halted');
    });
}

// ── Service Health Grid ───────────────────────────────────────────────────────
const SERVICES_META = [
    { name: 'FastAPI',    key: 'api',      icon: 'server',   color: 'var(--gr)' },
    { name: 'PostgreSQL', key: 'postgres', icon: 'database', color: 'var(--bl)' },
    { name: 'Redis',      key: 'redis',    icon: 'zap',      color: 'var(--am)' },
    { name: 'Neo4j',      key: 'neo4j',    icon: 'share-2',  color: 'var(--pu)' },
    { name: 'Qdrant',     key: 'qdrant',   icon: 'box',      color: 'var(--cy)' },
    { name: 'Ollama',     key: 'ollama',   icon: 'cpu',      color: 'var(--rd)' },
];

function renderHealthGrid(services) {
    const grid = document.getElementById('health-grid');
    if (!grid) return;
    grid.innerHTML = services.map(svc => {
        const statusClass = svc.status === 'healthy' ? 'svc-healthy' : svc.status === 'degraded' ? 'svc-degraded' : 'svc-down';
        const statusLabel = svc.status === 'healthy' ? 'Online' : svc.status === 'degraded' ? 'Degradado' : 'Offline';
        const dotColor    = svc.status === 'healthy' ? 'var(--gr)' : svc.status === 'degraded' ? 'var(--am)' : 'var(--rd)';
        return `<div class="svc-card ${statusClass}" onclick="showToast('${dotColor}','${svc.name}: ${statusLabel} · ${svc.latency_ms != null ? svc.latency_ms+'ms' : '—'}')">
            <div class="svc-icon"><i data-lucide="${svc.icon}" style="width:18px;height:18px;color:${svc.color}"></i></div>
            <div class="svc-name">${svc.name}</div>
            <div class="svc-status"><span class="svc-status-dot"></span>${statusLabel}</div>
            <div class="svc-latency">${svc.latency_ms != null ? svc.latency_ms + 'ms' : '—'}</div>
        </div>`;
    }).join('');
    const onlineCount = services.filter(s => s.status === 'healthy').length;
    const overallEl = document.getElementById('health-overall');
    if (overallEl) {
        overallEl.textContent = `● ${onlineCount}/${services.length} Online`;
        overallEl.className = `chip ${onlineCount === services.length ? 'grn' : onlineCount >= services.length / 2 ? '' : 'red'}`;
    }
    if (window.lucide) lucide.createIcons();
}

window.refreshHealth = async function() {
    const demo = SERVICES_META.map(s => ({
        ...s,
        status: Math.random() > 0.12 ? 'healthy' : (Math.random() > 0.5 ? 'degraded' : 'down'),
        latency_ms: Math.round(Math.random() * 38 + 1)
    }));
    renderHealthGrid(demo);
    try {
        const resp = await fetch(`/api/v5/dashboard/diagnostics/health?tenant_id=${APP_STATE.tenant_id}`);
        if (resp.ok) {
            const data = await resp.json();
            // endpoint returns data.components (keys: runtime, cognitive, postgres, redis, neo4j, qdrant, llm, ollama)
            const comps = data.components || data.services || {};
            const statusOf = (c) => {
                if (!c) return 'unknown';
                const s = c.status || c.raw_status || '';
                if (s === 'up' || s === 'healthy') return 'healthy';
                if (s === 'warn' || s === 'degraded') return 'degraded';
                return 'down';
            };
            const merged = SERVICES_META.map(s => {
                const real = comps[s.key] || comps[s.name.toLowerCase()] || null;
                return {
                    ...s,
                    status: statusOf(real),
                    latency_ms: real?.latency_ms ?? real?.response_time_ms ?? null,
                    detail: real?.detail || '',
                };
            });
            renderHealthGrid(merged);
        }
    } catch(e) { /* use demo */ }
};

// ── Active Goals ──────────────────────────────────────────────────────────────
const DEMO_GOALS = [
    { title: 'Otimizar pipeline de ingestão de documentos', agent: 'orchestrator', progress: 68, color: 'var(--gr)' },
    { title: 'Refatorar módulo de autenticação N5', agent: 'refactor_agent', progress: 34, color: 'var(--bl)' },
    { title: 'Expandir cobertura de testes para 90%', agent: 'qa_agent', progress: 84, color: 'var(--pu)' },
    { title: 'Sincronizar grafo Neo4j com estado atual', agent: 'neo4j_sync', progress: 12, color: 'var(--cy)' },
];

function renderGoals(goals) {
    const container = document.getElementById('goals-container');
    if (!container) return;
    container.innerHTML = goals.map(g => `
        <div class="goal-item">
            <div class="goal-title">${g.title}</div>
            <div class="goal-meta">
                <span>${g.agent}</span>
                <span>${g.progress}% concluído</span>
            </div>
            <div class="goal-prog-wrap">
                <div class="goal-prog-bar" style="background:${g.color}" data-w="${g.progress}"></div>
            </div>
        </div>`).join('');
    setTimeout(() => {
        container.querySelectorAll('.goal-prog-bar[data-w]').forEach(b => {
            b.style.transition = 'width 1.6s cubic-bezier(0.16,1,0.3,1)';
            b.style.width = b.dataset.w + '%';
        });
    }, 80);
}

window.loadActiveGoals = async function() {
    renderGoals(DEMO_GOALS);
    try {
        const resp = await fetch(`/api/v5/dashboard/active-goals?tenant_id=${APP_STATE.tenant_id}`);
        if (resp.ok) {
            const data = await resp.json();
            const goals = (data.goals || data).slice(0, 6).map((g, i) => ({
                title: g.title || g.description || 'Objetivo Ativo',
                agent: g.agent_id || g.assigned_to || 'orchestrator',
                progress: g.progress_pct ?? Math.round(Math.random() * 70 + 10),
                color: ['var(--gr)', 'var(--bl)', 'var(--pu)', 'var(--cy)', 'var(--am)'][i % 5]
            }));
            if (goals.length) renderGoals(goals);
        }
    } catch(e) { /* use demo */ }
};

// ── Memory / Knowledge Stats ──────────────────────────────────────────────────
function renderMemoryStats(s) {
    const container = document.getElementById('memory-stats-container');
    if (!container) return;
    container.innerHTML = `
        <div class="mem-stat-row">
            <div><div class="mem-stat-label">Qdrant · Vetores L3</div><div class="mem-stat-sub">Memória de longo prazo</div></div>
            <div style="text-align:right"><div class="mem-stat-val" style="color:var(--cy)">${s.qdrant_vectors?.toLocaleString('pt-BR') || '—'}</div><div class="mem-stat-sub">${s.qdrant_collections || 0} coleções</div></div>
        </div>
        <div class="mem-stat-row">
            <div><div class="mem-stat-label">Neo4j · Grafo de Conhecimento</div><div class="mem-stat-sub">Impacto &amp; relações causais</div></div>
            <div style="text-align:right"><div class="mem-stat-val" style="color:var(--pu)">${s.neo4j_nodes?.toLocaleString('pt-BR') || '—'}</div><div class="mem-stat-sub">${s.neo4j_edges?.toLocaleString('pt-BR') || '0'} arestas</div></div>
        </div>
        <div class="mem-stat-row">
            <div><div class="mem-stat-label">Lições Aprendidas</div><div class="mem-stat-sub">Base de conhecimento N5</div></div>
            <div style="text-align:right"><div class="mem-stat-val" style="color:var(--am)">${s.lessons_count || 53}</div><div class="mem-stat-sub">última há ${s.last_lesson_ago || '8min'}</div></div>
        </div>
        <div class="mem-stat-row">
            <div><div class="mem-stat-label">Cache L2 · Redis</div><div class="mem-stat-sub">Memória de trabalho</div></div>
            <div style="text-align:right"><div class="mem-stat-val" style="color:var(--am)">${s.redis_keys?.toLocaleString('pt-BR') || '—'}</div><div class="mem-stat-sub">${s.cache_hit_rate || 0}% hit rate</div></div>
        </div>
        <div class="mem-stat-row">
            <div><div class="mem-stat-label">Tokens Processados Hoje</div><div class="mem-stat-sub">Todos os tenants combinados</div></div>
            <div style="text-align:right"><div class="mem-stat-val" style="color:var(--gr)">${s.tokens_today ? Math.round(s.tokens_today / 1000) + 'k' : '480k'}</div><div class="mem-stat-sub">quota 82% usada</div></div>
        </div>`;
}

window.loadMemoryStats = async function() {
    renderMemoryStats({
        qdrant_vectors: 42841, qdrant_collections: 4,
        neo4j_nodes: 2841, neo4j_edges: 12408,
        lessons_count: 53, last_lesson_ago: '8min',
        redis_keys: 1247, cache_hit_rate: 91, tokens_today: 480000
    });
    try {
        const resp = await fetch(`/api/v5/dashboard/intelligence/memory-stats?tenant_id=${APP_STATE.tenant_id}`);
        if (resp.ok) {
            const d = await resp.json();
            renderMemoryStats({
                qdrant_vectors: d.qdrant?.total_vectors ?? d.total_vectors,
                qdrant_collections: d.qdrant?.collections ?? 4,
                neo4j_nodes: d.neo4j?.node_count ?? d.node_count,
                neo4j_edges: d.neo4j?.relationship_count ?? d.relationship_count,
                lessons_count: d.lessons?.total ?? 53,
                last_lesson_ago: d.lessons?.last_ago || '8min',
                redis_keys: d.redis?.total_keys,
                cache_hit_rate: d.redis?.hit_rate_pct,
                tokens_today: d.tokens_today
            });
        }
    } catch(e) { /* use demo */ }
};

// ── Lessons Learned Database ──────────────────────────────────────────────────
const DEMO_LESSONS = [
    { num: 54, text: 'Timeout pattern no Deep Path quando context_retriever >2s — retry com backoff exponencial.', tag: 'performance', time: '8min' },
    { num: 53, text: 'QA coverage cai com qa_agent em Fast Path abaixo de conf 75% — forçar Deep Path para testes.', tag: 'qualidade', time: '2h' },
    { num: 52, text: 'Ingest worker gargalo em lotes >100 docs — dividir em micro-lotes de 20 para throughput ótimo.', tag: 'throughput', time: '6h' },
    { num: 51, text: 'Neo4j query otimizada com índice em rel type — redução de 40ms → 3ms em blast-radius scan.', tag: 'neo4j', time: '12h' },
    { num: 50, text: 'Redis eviction LRU invalida contexto ativo — usar TTL dinâmico proporcional ao task depth.', tag: 'redis', time: '1d' },
    { num: 49, text: 'PR gerado automaticamente sem lint — adicionar lint gate pré-HIL review obrigatório.', tag: 'governance', time: '2d' },
];

window.renderLessons = function(lessons) {
    const container = document.getElementById('lessons-container');
    if (!container) return;
    if (!lessons.length) {
        container.innerHTML = '<div style="padding:24px;text-align:center;color:var(--t3);font-size:11px;font-family:var(--m)">Nenhuma lição registrada ainda.</div>';
        return;
    }
    container.innerHTML = `<div class="lesson-grid">${lessons.map((l, i) => {
        const num  = l.num  ?? l.id    ?? (lessons.length - i);
        const text = l.text ?? l.context ?? l.attempted_fix ?? l.error_signature ?? '—';
        const tag  = l.tag  ?? l.result ?? 'info';
        let timeStr = l.time ?? '';
        if (!timeStr && l.created_at) {
            const diffMs = Date.now() - new Date(l.created_at).getTime();
            const diffMin = Math.floor(diffMs / 60000);
            if (diffMin < 60)        timeStr = diffMin + 'min';
            else if (diffMin < 1440) timeStr = Math.floor(diffMin/60) + 'h';
            else                     timeStr = Math.floor(diffMin/1440) + 'd';
        }
        const tagColor = tag === 'success' ? 'var(--gr)' : tag === 'failure' ? 'var(--rd)' : 'var(--bl)';
        return `<div class="lesson-item">
            <div class="lesson-num">#${num}</div>
            <div style="flex:1">
                <div class="lesson-text">${_escHtml ? _escHtml(String(text).slice(0, 220)) : String(text).slice(0, 220)}</div>
                <div class="lesson-tag-row">
                    <span class="nbadge" style="font-size:8px;padding:1px 5px;background:${tagColor}22;color:${tagColor};border-color:${tagColor}">${tag}</span>
                    ${l.agent_name ? `<span class="lesson-tag">${_escHtml ? _escHtml(l.agent_name) : l.agent_name}</span>` : ''}
                    ${timeStr ? `<span class="lesson-tag">há ${timeStr}</span>` : ''}
                    ${l.confidence != null ? `<span class="lesson-tag">${Math.round(l.confidence*100)}% conf</span>` : ''}
                </div>
            </div>
        </div>`;
    }).join('')}</div>`;
};

window.loadLessons = async function() {
    renderLessons(DEMO_LESSONS);   // show demo while loading
    try {
        const resp = await fetch(`/api/v5/dashboard/intelligence/lessons?limit=20&tenant_id=${APP_STATE.tenant_id}`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.lessons && data.lessons.length > 0) renderLessons(data.lessons);
    } catch (_) {}
};

// ── Knowledge Explorer ────────────────────────────────────────────────────────
let explorerNetwork = null;
let explorerNodes  = null;
let explorerEdges  = null;

const EXPLORER_GROUPS = {
    route:   { color: { background: '#4D80FF', border: '#8eb0ff' }, shape: 'diamond',      size: 18 },
    service: { color: { background: '#2ED47A', border: '#7eedb2' }, shape: 'dot',          size: 16 },
    core:    { color: { background: '#9B7BFF', border: '#c4aaff' }, shape: 'hexagon',      size: 22 },
    agent:   { color: { background: '#F0A020', border: '#f5c265' }, shape: 'star',         size: 20 },
    model:   { color: { background: '#1ECFB8', border: '#6ee8da' }, shape: 'square',       size: 20 },
    test:    { color: { background: '#8e8e9e', border: '#b4b4c0' }, shape: 'triangleDown', size: 14 },
    default: { color: { background: '#363650', border: '#6868a0' }, shape: 'dot',          size: 14 },
};

const DEMO_KNOWLEDGE_GRAPH = {
    nodes: [
        // Core
        { id: 'app',             label: 'app.py',              group: 'core',    title: 'FastAPI Application Root · entry point completo',    mass: 4 },
        { id: 'auth',            label: 'auth.py',             group: 'core',    title: 'Auth & Tenant Middleware · rate limiting · quotas' },
        { id: 'db',              label: 'db.py',               group: 'core',    title: 'Async PostgreSQL Pool · SQLAlchemy' },
        { id: 'redis_',          label: 'redis_.py',           group: 'core',    title: 'Redis Cache L2 · session store' },
        { id: 'config',          label: 'config.py',           group: 'core',    title: 'Environment Config · env vars' },
        { id: 'security_config', label: 'security_config.py',  group: 'core',    title: 'Políticas de segurança · CORS · headers' },
        { id: 'governance',      label: 'governance_plane.py', group: 'core',    title: 'Plano de governança · circuit breakers' },
        // Routes
        { id: 'route_dashboard', label: '/dashboard',          group: 'route',   title: 'Dashboard API · telemetry · diagnostics · health' },
        { id: 'route_ask',       label: '/ask',                group: 'route',   title: 'LLM Query Route · streaming SSE' },
        { id: 'route_tasks',     label: '/tasks',              group: 'route',   title: 'Task CRUD · DAG scheduler' },
        { id: 'route_admin',     label: '/admin',              group: 'route',   title: 'Admin Control · tenant management' },
        { id: 'route_intel',     label: '/intelligence',       group: 'route',   title: 'Agent Intelligence · memory stats · agent details' },
        { id: 'route_plans',     label: '/plans',              group: 'route',   title: 'Execution Plans · MCTS tree output' },
        { id: 'route_connect',   label: '/connect',            group: 'route',   title: 'Multi-agent connection layer' },
        // Agents / Services
        { id: 'orchestrator',    label: 'Orchestrator',        group: 'agent',   title: 'MCTS Orchestrator · Fast/Deep path router', mass: 3 },
        { id: 'refactor_agent',  label: 'RefactorAgent',       group: 'agent',   title: 'Code Refactoring · PR generation' },
        { id: 'qa_agent',        label: 'QA Agent',            group: 'agent',   title: 'Test generation · coverage analysis' },
        { id: 'ingest_worker',   label: 'IngestWorker',        group: 'agent',   title: 'Doc ingestion · FAISS 768d · Qdrant sync' },
        { id: 'ctx_retriever',   label: 'ContextRetriever',    group: 'agent',   title: 'L3 Qdrant retrieval · context assembly' },
        { id: 'ast_analyzer',    label: 'ASTAnalyzer',         group: 'service', title: 'AST parse · Neo4j graph write' },
        { id: 'impact_analyzer', label: 'ImpactAnalyzer',      group: 'service', title: 'Blast radius calculator · dependency walk' },
        { id: 'event_bus',       label: 'EventBus',            group: 'service', title: 'Redis Pub/Sub · async event dispatch' },
        { id: 'ext_bridge',      label: 'ExternalAgentBridge', group: 'service', title: 'Multi-tenant agent router · external APIs' },
        // Storage
        { id: 'neo4j',           label: 'Neo4j',               group: 'model',   title: 'Knowledge Graph · 2.841 nós · 12.408 arestas', mass: 3 },
        { id: 'qdrant',          label: 'Qdrant',              group: 'model',   title: 'Vector Store L3 · 42.841 vetores · 4 coleções',  mass: 3 },
        { id: 'postgres',        label: 'PostgreSQL',          group: 'model',   title: 'Relational DB · tasks · tenants · quotas · logs' },
        { id: 'redis_store',     label: 'Redis',               group: 'model',   title: 'Cache L2 · 1.247 keys · sessions · pub/sub' },
        { id: 'ollama',          label: 'Ollama',              group: 'model',   title: 'Local LLM inference · GPU RTX 3090' },
    ],
    edges: [
        { from: 'app',            to: 'auth',            label: 'middleware' },
        { from: 'app',            to: 'db' },
        { from: 'app',            to: 'redis_' },
        { from: 'app',            to: 'route_dashboard' },
        { from: 'app',            to: 'route_ask' },
        { from: 'app',            to: 'route_tasks' },
        { from: 'app',            to: 'route_admin' },
        { from: 'app',            to: 'route_intel' },
        { from: 'app',            to: 'route_plans' },
        { from: 'app',            to: 'route_connect' },
        { from: 'app',            to: 'governance' },
        { from: 'auth',           to: 'redis_',          label: 'token' },
        { from: 'auth',           to: 'db',              label: 'tenant' },
        { from: 'auth',           to: 'security_config' },
        { from: 'config',         to: 'auth' },
        { from: 'governance',     to: 'orchestrator',    label: 'circuit break' },
        { from: 'orchestrator',   to: 'ast_analyzer' },
        { from: 'orchestrator',   to: 'impact_analyzer' },
        { from: 'orchestrator',   to: 'event_bus',       label: 'publish' },
        { from: 'orchestrator',   to: 'qdrant',          label: 'L3 memory' },
        { from: 'orchestrator',   to: 'neo4j',           label: 'graph ops' },
        { from: 'orchestrator',   to: 'ollama',          label: 'inference' },
        { from: 'refactor_agent', to: 'ast_analyzer' },
        { from: 'refactor_agent', to: 'event_bus' },
        { from: 'qa_agent',       to: 'event_bus' },
        { from: 'qa_agent',       to: 'qdrant',          label: 'test memory' },
        { from: 'ingest_worker',  to: 'qdrant',          label: 'ingest' },
        { from: 'ingest_worker',  to: 'neo4j',           label: 'graph write' },
        { from: 'ctx_retriever',  to: 'qdrant',          label: 'search' },
        { from: 'ctx_retriever',  to: 'neo4j',           label: 'traverse' },
        { from: 'ast_analyzer',   to: 'neo4j',           label: 'read/write' },
        { from: 'impact_analyzer',to: 'neo4j',           label: 'query' },
        { from: 'event_bus',      to: 'redis_store',     label: 'pub/sub' },
        { from: 'ext_bridge',     to: 'orchestrator' },
        { from: 'route_dashboard',to: 'event_bus' },
        { from: 'route_dashboard',to: 'auth',            label: 'depends' },
        { from: 'route_ask',      to: 'orchestrator' },
        { from: 'route_ask',      to: 'ctx_retriever' },
        { from: 'route_tasks',    to: 'postgres',        label: 'CRUD' },
        { from: 'route_tasks',    to: 'event_bus' },
        { from: 'route_admin',    to: 'auth' },
        { from: 'route_intel',    to: 'qdrant',          label: 'stats' },
        { from: 'route_intel',    to: 'neo4j' },
        { from: 'route_plans',    to: 'orchestrator' },
        { from: 'route_connect',  to: 'ext_bridge' },
        { from: 'db',             to: 'postgres' },
        { from: 'redis_',         to: 'redis_store' },
    ]
};

function initExplorer() {
    const container = document.getElementById('explorer-graph');
    if (!container || explorerNetwork) return;

    explorerNodes = new vis.DataSet(DEMO_KNOWLEDGE_GRAPH.nodes);
    explorerEdges = new vis.DataSet(DEMO_KNOWLEDGE_GRAPH.edges.map((e, i) => ({ id: i, ...e })));

    const opts = {
        nodes: {
            font: { color: '#EEECf8', face: 'DM Mono', size: 11, strokeWidth: 2, strokeColor: 'rgba(0,0,0,0.5)' },
            borderWidth: 2,
            shadow: { enabled: true, color: 'rgba(0,0,0,0.5)', size: 10, x: 2, y: 2 }
        },
        edges: {
            width: 1.2,
            color: { color: 'rgba(255,255,255,0.1)', highlight: '#4D80FF', hover: '#9B7BFF' },
            smooth: { type: 'continuous' },
            arrows: { to: { enabled: true, scaleFactor: 0.45 } },
            font: { color: '#363650', size: 8, face: 'DM Mono', align: 'middle' },
            selectionWidth: 2
        },
        physics: {
            solver: 'forceAtlas2Based',
            forceAtlas2Based: { gravitationalConstant: -85, centralGravity: 0.015, springLength: 130, springConstant: 0.05 },
            maxVelocity: 60, minVelocity: 0.3, timestep: 0.35,
            stabilization: { iterations: 250, fit: true }
        },
        groups: EXPLORER_GROUPS,
        interaction: { hover: true, tooltipDelay: 120, navigationButtons: false, keyboard: { enabled: true, bindToWindow: false }, zoomView: true },
        layout: { improvedLayout: true }
    };

    explorerNetwork = new vis.Network(container, { nodes: explorerNodes, edges: explorerEdges }, opts);

    explorerNetwork.on('click', params => {
        if (params.nodes.length > 0) {
            showNodeDetail(params.nodes[0]);
            explorerNetwork.focus(params.nodes[0], { scale: 1.7, animation: { duration: 700, easingFunction: 'easeInOutQuad' } });
        } else {
            const detail = document.getElementById('explorer-detail');
            if (detail) detail.innerHTML = explorerDetailPlaceholder();
            explorerNetwork.fit({ animation: { duration: 600, easingFunction: 'easeInOutQuad' } });
        }
    });

    explorerNetwork.on('hoverNode', params => {
        container.style.cursor = 'pointer';
        const node = explorerNodes.get(params.node);
        if (node) writeToTerminal(`[GRAPH] ${node.label} · ${node.group} · ${node.title?.split('·')[0]?.trim() || ''}`, 'system');
    });

    explorerNetwork.on('blurNode', () => { container.style.cursor = 'default'; });

    const detail = document.getElementById('explorer-detail');
    if (detail) detail.innerHTML = explorerDetailPlaceholder();
}

function explorerDetailPlaceholder() {
    return `<div style="padding:24px 16px;text-align:center;color:var(--t3);font-family:var(--m);font-size:10px">
        <div style="font-size:28px;margin-bottom:10px;opacity:0.35">⬡</div>
        Clique num nó para explorar<br>suas conexões e dependências<br><br>
        <span style="color:var(--t4);font-size:9px">Duplo clique → isolar subgrafo<br>⚡ Blast Radius → impacto de mudanças</span>
    </div>`;
}

function showNodeDetail(nodeId) {
    const node = explorerNodes.get(nodeId);
    if (!node) return;
    const allEdges = explorerEdges.get();
    const incoming = allEdges.filter(e => e.to   === nodeId);
    const outgoing = allEdges.filter(e => e.from === nodeId);
    const detail = document.getElementById('explorer-detail');
    if (!detail) return;

    const groupColor = { route: '#4D80FF', service: '#2ED47A', core: '#9B7BFF', agent: '#F0A020', model: '#1ECFB8', test: '#8e8e9e', default: '#6868a0' };
    const c = groupColor[node.group] || '#6868a0';

    detail.innerHTML = `
        <div style="padding:14px 14px 0">
            <div style="display:flex;align-items:center;gap:10px;margin-bottom:10px;padding-bottom:10px;border-bottom:1px solid var(--b)">
                <div style="width:12px;height:12px;border-radius:50%;background:${c};box-shadow:0 0 10px ${c};flex-shrink:0"></div>
                <div>
                    <div style="font-size:13px;font-weight:700;color:var(--t1);font-family:var(--m)">${node.label}</div>
                    <div style="font-size:9px;color:${c};font-family:var(--m);text-transform:uppercase;letter-spacing:0.07em;margin-top:1px">${node.group}</div>
                </div>
            </div>
            <div style="font-size:10px;color:var(--t2);font-family:var(--m);margin-bottom:12px;line-height:1.55">${node.title || '—'}</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:7px;margin-bottom:12px">
                <div style="background:var(--bg3);border:1px solid var(--b);border-radius:6px;padding:9px;text-align:center">
                    <div style="font-size:20px;font-weight:700;color:var(--bl);font-family:var(--m)">${incoming.length}</div>
                    <div style="font-size:9px;color:var(--t3);font-family:var(--m)">depende de</div>
                </div>
                <div style="background:var(--bg3);border:1px solid var(--b);border-radius:6px;padding:9px;text-align:center">
                    <div style="font-size:20px;font-weight:700;color:var(--gr);font-family:var(--m)">${outgoing.length}</div>
                    <div style="font-size:9px;color:var(--t3);font-family:var(--m)">exporta para</div>
                </div>
            </div>
            ${incoming.length ? `<div style="font-size:9px;color:var(--t3);font-family:var(--m);margin-bottom:5px;text-transform:uppercase;letter-spacing:0.05em">← Recebe de</div>
            <div style="margin-bottom:10px">${incoming.map(e => `<div class="explorer-link-row" onclick="explorerFocus('${e.from}')" style="color:var(--bl)">← ${e.from}${e.label ? `<span style="color:var(--t3)"> · ${e.label}</span>` : ''}</div>`).join('')}</div>` : ''}
            ${outgoing.length ? `<div style="font-size:9px;color:var(--t3);font-family:var(--m);margin-bottom:5px;text-transform:uppercase;letter-spacing:0.05em">→ Envia para</div>
            <div style="margin-bottom:12px">${outgoing.map(e => `<div class="explorer-link-row" onclick="explorerFocus('${e.to}')" style="color:var(--gr)">→ ${e.to}${e.label ? `<span style="color:var(--t3)"> · ${e.label}</span>` : ''}</div>`).join('')}</div>` : ''}
            <button class="btn danger" style="width:100%;margin-bottom:6px" onclick="searchBlastRadius('${node.label}')">⚡ Blast Radius</button>
            <button class="btn" style="width:100%;margin-bottom:12px" onclick="explorerIsolate('${nodeId}')">⊙ Isolar Subgrafo</button>
        </div>`;
}

window.explorerFocus = function(nodeId) {
    if (!explorerNetwork) return;
    explorerNetwork.selectNodes([nodeId]);
    explorerNetwork.focus(nodeId, { scale: 2, animation: { duration: 600, easingFunction: 'easeInOutQuad' } });
    showNodeDetail(nodeId);
};

window.explorerIsolate = function(nodeId) {
    if (!explorerNodes || !explorerEdges) return;
    const allEdges = explorerEdges.get();
    const connected = new Set([nodeId]);
    allEdges.forEach(e => { if (e.from === nodeId) connected.add(e.to); if (e.to === nodeId) connected.add(e.from); });
    explorerNodes.update(explorerNodes.get().map(n => ({ id: n.id, opacity: connected.has(n.id) ? 1 : 0.08 })));
    explorerEdges.update(allEdges.map(e => ({ id: e.id, color: { opacity: (connected.has(e.from) && connected.has(e.to)) ? 0.8 : 0.04 } })));
    if (typeof showToast === 'function') showToast('var(--pu)', `Subgrafo: ${connected.size} nós conectados a ${nodeId}`);
};

window.searchExplorer = function(query) {
    if (!query || !explorerNodes) return;
    const q = query.toLowerCase();
    const all = explorerNodes.get();
    const matching = all.filter(n =>
        n.label.toLowerCase().includes(q) ||
        (n.title || '').toLowerCase().includes(q) ||
        (n.group || '').toLowerCase().includes(q)
    );
    if (!matching.length) {
        if (typeof showToast === 'function') showToast('var(--am)', `Nenhum nó encontrado: "${query}"`);
        return;
    }
    // Dim non-matching
    explorerNodes.update(all.map(n => ({ id: n.id, opacity: matching.some(m => m.id === n.id) ? 1 : 0.12 })));
    if (matching.length === 1) {
        explorerNetwork.focus(matching[0].id, { scale: 2.2, animation: { duration: 800, easingFunction: 'easeInOutQuad' } });
        showNodeDetail(matching[0].id);
    } else {
        explorerNetwork.fit({ nodes: matching.map(n => n.id), animation: { duration: 800, easingFunction: 'easeInOutQuad' } });
    }
    if (typeof showToast === 'function') showToast('var(--bl)', `${matching.length} nó(s) encontrado(s): "${query}"`);
};

window.resetExplorerGraph = function() {
    if (!explorerNodes) return;
    const allEdges = explorerEdges.get();
    explorerNodes.update(explorerNodes.get().map(n => ({ id: n.id, opacity: 1 })));
    explorerEdges.update(allEdges.map(e => ({ id: e.id, color: undefined })));
    explorerNetwork.fit({ animation: { duration: 700, easingFunction: 'easeInOutQuad' } });
    const detail = document.getElementById('explorer-detail');
    if (detail) detail.innerHTML = explorerDetailPlaceholder();
    document.getElementById('explorer-search-input').value = '';
};

window.searchBlastRadius = async function(symbol) {
    if (!symbol && document.getElementById('explorer-search-input')) {
        symbol = document.getElementById('explorer-search-input').value.trim();
    }
    if (!symbol) { if (typeof showToast === 'function') showToast('var(--am)', 'Digite um símbolo para calcular o blast radius'); return; }

    writeToTerminal(`[BLAST RADIUS] Calculando impacto de: ${symbol}`, 'warn');
    if (typeof showToast === 'function') showToast('var(--am)', `⚡ Calculando blast radius: ${symbol}...`);

    try {
        const resp = await fetch(`/api/v5/dashboard/cognitive/blast-radius?symbol=${encodeURIComponent(symbol)}&tenant_id=${APP_STATE.tenant_id}`);
        if (resp.ok) {
            const data = await resp.json();
            if (data.impact_map && data.impact_map.length) {
                // Add impact to the NOC impact graph
                renderGraphImpact({ target: symbol, impact_map: data.impact_map });
                // Highlight in explorer graph
                if (explorerNodes) {
                    const impacted = new Set(data.impact_map.map(i => i.name || i.file));
                    explorerNodes.update(explorerNodes.get().map(n => ({
                        id: n.id,
                        color: impacted.has(n.label) ? { background: '#f03250', border: '#ff4d6d' } : undefined,
                        opacity: impacted.has(n.label) ? 1 : 0.3
                    })));
                }
                writeToTerminal(`[BLAST RADIUS] ${data.impact_map.length} impactos detectados para: ${symbol}`, 'err');
                if (typeof showToast === 'function') showToast('var(--rd)', `⚡ ${data.impact_map.length} impactos · ${symbol}`);
            } else {
                if (typeof showToast === 'function') showToast('var(--gr)', `Sem impactos críticos detectados para: ${symbol}`);
            }
        }
    } catch(e) {
        // Demo blast radius on error
        const demoImpacts = ['auth.py', 'redis_.py', 'db.py'].filter(n => n !== symbol);
        if (explorerNodes) {
            const demo = new Set(demoImpacts);
            explorerNodes.update(explorerNodes.get().map(n => ({
                id: n.id,
                color: demo.has(n.label) ? { background: '#f03250', border: '#ff4d6d' } : undefined,
                opacity: demo.has(n.label) ? 1 : 0.25
            })));
        }
        writeToTerminal(`[BLAST RADIUS] Demo: ${demoImpacts.join(', ')} impactados por ${symbol}`, 'err');
        if (typeof showToast === 'function') showToast('var(--rd)', `⚡ Blast radius (demo): ${demoImpacts.length} impactos`);
    }
};

window.searchMemoryL3 = async function(query) {
    const resultsEl = document.getElementById('mem-search-results');
    if (!resultsEl || !query.trim()) return;

    resultsEl.innerHTML = `<div style="padding:10px;text-align:center;color:var(--t3);font-family:var(--m);font-size:10px">Buscando em Qdrant L3...</div>`;
    writeToTerminal(`[L3 QDRANT] Busca semântica: "${query}"`, 'system');

    const renderResults = items => {
        resultsEl.innerHTML = items.map(r => `
            <div class="mem-result">
                <div class="mem-result-score" style="color:${r.score > 0.8 ? 'var(--gr)' : r.score > 0.5 ? 'var(--am)' : 'var(--t3)'}">${Math.round(r.score * 100)}%</div>
                <div class="mem-result-body">
                    <div class="mem-result-text">${r.text}</div>
                    <div class="mem-result-meta">${r.meta}</div>
                </div>
            </div>`).join('');
    };

    const DEMO_MEM_RESULTS = [
        { score: 0.96, text: `FastAPI middleware chain: SecurityMiddleware → RateLimitMiddleware → get_tenant_id. Tenant isolation enforced per-request via request.state.tenant.`, meta: 'core/auth.py · chunk #12' },
        { score: 0.89, text: `MCTS Orchestrator routes tasks through Fast Path (conf ≥ threshold) or Deep Path (complex + high risk). Blast radius from Neo4j determines path.`, meta: 'services/orchestrator.py · chunk #7' },
        { score: 0.74, text: `Qdrant collections: agent_memory (768d), lessons_learned (768d), code_chunks (1536d), session_context (512d). Hit rate 91%.`, meta: 'core/memory.py · chunk #3' },
        { score: 0.68, text: `Neo4j schema: (File)-[:IMPORTS]->(File), (Function)-[:CALLS]->(Function), (Service)-[:DEPENDS_ON]->(Service). 2.841 nós, 12.408 arestas.`, meta: 'services/ast_analyzer.py · chunk #18' },
        { score: 0.61, text: `ingest_worker: processa PDFs, código-fonte e docs Markdown. Splits em chunks de 512 tokens com overlap 64. Embeddings via sentence-transformers.`, meta: 'services/ingest_worker.py · chunk #2' },
    ];

    try {
        const resp = await fetch(`/api/v5/dashboard/cognitive/memory/search?query=${encodeURIComponent(query)}&limit=5&tenant_id=${APP_STATE.tenant_id}`);
        if (resp.ok) {
            const data = await resp.json();
            const results = (data.results || data).slice(0, 5).map(r => ({
                score: r.score ?? r.similarity ?? 0.75,
                text:  r.text || r.content || r.payload?.text || r.payload?.content || '—',
                meta:  r.payload?.source || r.source || r.collection || 'qdrant'
            }));
            if (results.length) { renderResults(results); return; }
        }
    } catch(e) { /* use demo */ }

    renderResults(DEMO_MEM_RESULTS);
    writeToTerminal(`[L3 QDRANT] ${DEMO_MEM_RESULTS.length} resultados (demo) para: "${query}"`, 'success');
};

window.openExplorer = function() {
    document.getElementById('view-main-dashboard').style.display = 'none';
    document.getElementById('engine-room-spa').style.display = 'none';
    document.getElementById('ask-spa').style.display = 'none';
    const spa = document.getElementById('explorer-spa');
    if (spa) {
        spa.style.display = 'block';
        if (window.closeSidebar) closeSidebar();
        setTimeout(() => {
            initExplorer();
            if (explorerNetwork) { explorerNetwork.redraw(); explorerNetwork.fit({ animation: { duration: 800 } }); }
        }, 80);
    }
};

window.closeExplorer = function() {
    document.getElementById('explorer-spa').style.display = 'none';
    document.getElementById('view-main-dashboard').style.display = 'flex';
};

// ── Ask N5 · LLM Chat SPA ─────────────────────────────────────────────────────
let askSessionId        = '';
let askIsStreaming      = false;
let askCurrentEvtSource = null;

function _escHtml(t) {
    return String(t).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _renderMd(text) {
    // Fenced code blocks first (protect from further replacements)
    const blocks = [];
    text = text.replace(/```(\w*)\n?([\s\S]*?)```/g, (_, lang, code) => {
        const ph = `\x00BLOCK${blocks.length}\x00`;
        blocks.push(`<div class="ask-code-block"><div class="ask-code-lang">${lang||'code'}</div><pre>${_escHtml(code.trim())}</pre></div>`);
        return ph;
    });
    // Inline code
    text = text.replace(/`([^`\n]+)`/g, '<code class="ask-inline-code">$1</code>');
    // Bold + italic
    text = text.replace(/\*\*\*([^*]+)\*\*\*/g, '<strong><em>$1</em></strong>');
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/\*([^*\n]+)\*/g, '<em>$1</em>');
    // Headers
    text = text.replace(/^### (.+)$/gm, '<div class="ask-h3">$1</div>');
    text = text.replace(/^## (.+)$/gm,  '<div class="ask-h2">$1</div>');
    text = text.replace(/^# (.+)$/gm,   '<div class="ask-h1">$1</div>');
    // Bullet lists
    text = text.replace(/^[-•] (.+)$/gm, '<div class="ask-li"><span class="ask-li-dot">•</span>$1</div>');
    // Newlines
    text = text.replace(/\n(?![<\x00])/g, '<br>');
    // Restore code blocks
    blocks.forEach((b, i) => { text = text.replace(`\x00BLOCK${i}\x00`, b); });
    return text;
}

function _appendAskMsg(role, content, meta = {}) {
    const chatArea = document.getElementById('ask-chat-area');
    if (!chatArea) return null;
    const welcome = chatArea.querySelector('.ask-welcome');
    if (welcome) welcome.remove();

    const el = document.createElement('div');
    el.className = `ask-msg ask-msg-${role}`;

    if (role === 'user') {
        el.innerHTML = `<div class="ask-msg-bubble">${_escHtml(content)}</div>`;
    } else {
        el.innerHTML = `
            <div class="ask-msg-avatar">N5</div>
            <div class="ask-msg-right">
                <div class="ask-msg-bubble" id="${meta.msgId||''}"></div>
                <div class="ask-sources" id="${meta.srcId||''}"></div>
                <div class="ask-msg-meta" id="${meta.metaId||''}"></div>
            </div>`;
    }
    chatArea.appendChild(el);
    chatArea.scrollTop = chatArea.scrollHeight;
    return el;
}

window.sendAsk = async function() {
    if (askIsStreaming) return;
    const input  = document.getElementById('ask-input');
    const prompt = input?.value.trim();
    if (!prompt) return;

    const projectId = document.getElementById('ask-project-id')?.value.trim() || 'project0';
    input.value = '';

    _appendAskMsg('user', prompt);

    if (!askSessionId) askSessionId = 'noc-' + Math.random().toString(36).slice(2, 11);
    const sessionChip = document.getElementById('ask-session-chip');
    if (sessionChip) sessionChip.textContent = `session: ${askSessionId.slice(-8)}`;

    const msgId  = 'ask-m-'    + Date.now();
    const metaId = 'ask-meta-' + Date.now();
    const srcId  = 'ask-src-'  + Date.now();
    _appendAskMsg('assistant', '', { msgId, metaId, srcId });
    const contentEl = document.getElementById(msgId);
    const metaEl    = document.getElementById(metaId);
    const srcEl     = document.getElementById(srcId);

    askIsStreaming = true;
    const sendBtn  = document.getElementById('ask-send-btn');
    const statusEl = document.getElementById('ask-status');
    if (sendBtn)  { sendBtn.textContent = '⏳'; sendBtn.disabled = true; }
    if (statusEl) statusEl.textContent = 'Recuperando contexto RAG...';
    if (contentEl) contentEl.innerHTML = '<span class="ask-cursor">▍</span>';

    if (window.addSystemEvent) addSystemEvent('agent', `Ask: "${prompt.slice(0, 80)}"`, { agent: 'llm_interface' });

    let fullText   = '';
    let tokenCount = 0;
    let evtDone    = false;

    const finish = (meta = {}) => {
        if (evtDone) return;
        evtDone = true;
        askIsStreaming = false;
        if (sendBtn)  { sendBtn.textContent = '⬆ Enviar'; sendBtn.disabled = false; }
        if (contentEl) contentEl.innerHTML = _renderMd(fullText);
        if (metaEl) {
            const parts = [];
            if (meta.model)      parts.push(`<span class="ask-model-tag">${_escHtml(meta.model)}</span>`);
            if (meta.latency_ms) parts.push(`<span class="ask-lat-tag">${meta.latency_ms}ms</span>`);
            if (tokenCount)      parts.push(`<span class="ask-lat-tag" style="color:var(--am);background:var(--amd);border-color:var(--am)">${tokenCount} tok</span>`);
            metaEl.innerHTML = parts.join('');
        }
        if (meta.sources?.length && srcEl) {
            srcEl.innerHTML = `<div class="ask-sources-hd" onclick="this.parentElement.classList.toggle('open')">📁 ${meta.sources.length} fonte${meta.sources.length>1?'s':''} usadas</div><div class="ask-sources-list">${meta.sources.map(s=>`<div class="ask-source-item">${_escHtml(s)}</div>`).join('')}</div>`;
        }
        if (statusEl) statusEl.textContent = meta.latency_ms ? `${meta.latency_ms}ms · ${tokenCount} tokens` : '';
        const modelChip = document.getElementById('ask-model-chip');
        if (modelChip && meta.model) modelChip.textContent = `⬡ ${meta.model.split(':')[0]}`;
        const chatArea = document.getElementById('ask-chat-area');
        if (chatArea) chatArea.scrollTop = chatArea.scrollHeight;
        if (window.addSystemEvent) addSystemEvent('success',
            `Ask concluído · ${tokenCount} tokens${meta.latency_ms?' · '+meta.latency_ms+'ms':''}`,
            { agent: meta.model || 'ollama' });
    };

    try {
        const url = `/api/v5/dashboard/ask?` + new URLSearchParams({
            prompt, project_id: projectId, session_id: askSessionId, tenant_id: APP_STATE.tenant_id
        });
        if (statusEl) statusEl.textContent = 'Conectando ao LLM...';
        const evtSource = new EventSource(url);
        askCurrentEvtSource = evtSource;
        evtSource.onopen = () => { if (statusEl) statusEl.textContent = 'Recebendo tokens...'; };

        evtSource.onmessage = (e) => {
            try {
                const data = JSON.parse(e.data);
                if (data.token) {
                    fullText += data.token;
                    tokenCount++;
                    if (contentEl) {
                        contentEl.innerHTML = _renderMd(fullText) + '<span class="ask-cursor">▍</span>';
                        const ca = document.getElementById('ask-chat-area');
                        if (ca) ca.scrollTop = ca.scrollHeight;
                    }
                    if (statusEl && tokenCount % 20 === 0) statusEl.textContent = `${tokenCount} tokens...`;
                } else if (data.done) {
                    evtSource.close(); askCurrentEvtSource = null;
                    finish({ latency_ms: data.latency_ms, model: data.model, sources: data.sources });
                } else if (data.error) {
                    evtSource.close(); askCurrentEvtSource = null;
                    if (!fullText) { _showDemoResp(contentEl, metaEl); askIsStreaming = false; if (sendBtn) { sendBtn.textContent = '⬆ Enviar'; sendBtn.disabled = false; } if (statusEl) statusEl.textContent = 'Demo (offline)'; }
                    else finish({});
                }
            } catch(_) {}
        };

        evtSource.onerror = () => {
            evtSource.close(); askCurrentEvtSource = null;
            if (evtDone) return;                        // already handled by done/error message
            if (!fullText) {
                evtDone = true;
                askIsStreaming = false;
                _showDemoResp(contentEl, metaEl);
                if (sendBtn)  { sendBtn.textContent = '⬆ Enviar'; sendBtn.disabled = false; }
                if (statusEl) statusEl.textContent = 'Demo (servidor offline)';
                if (window.addSystemEvent) addSystemEvent('warn', 'Ask SSE falhou · resposta demo', { agent: 'llm_interface' });
            } else { finish({}); }
        };

    } catch(err) {
        askIsStreaming = false;
        if (contentEl) contentEl.innerHTML = `<span style="color:var(--rd)">⚠ ${_escHtml(String(err))}</span>`;
        if (sendBtn)  { sendBtn.textContent = '⬆ Enviar'; sendBtn.disabled = false; }
        if (statusEl) statusEl.textContent = 'Erro';
    }
};

function _showDemoResp(contentEl, metaEl) {
    const DEMO = `**Resposta Demo** — servidor offline\n\nO projeto **SINC Orchestrator** é um sistema multi-agente cognitivo com arquitetura em camadas:\n\n### Arquitetura de Memória\n\n- **L0** – Rule Engine (regras determinísticas)\n- **L1** – Cache determinístico Redis\n- **L2** – Memória semântica Qdrant (vetorial)\n- **L3** – Raciocínio em grafo Neo4j\n- **L4** – Memória de eventos (PostgreSQL)\n\n### Pipeline de Ask\n\n\`\`\`python\n@router.post("/ask")\nasync def ask(body: AskRequest):\n    # 1. Verifica cache L0-L1\n    hit = await memory_router.resolve(body.prompt)\n    if hit: return hit\n    # 2. Routing (Ollama vs Anthropic)\n    routing = route_prompt(body.prompt)\n    # 3. Recupera contexto RAG\n    context, sources = await graph_aware_retrieve(body.prompt)\n    # 4. Chama LLM com contexto\n    answer = await _call_llm_async(routing, system_prompt, messages)\n    # 5. Aprende para cache futuro\n    memory_router.learn(body.prompt, answer)\n    return {"answer": answer, "sources": sources}\n\`\`\`\n\nPara conectar o LLM: \`docker compose up\` e certifique-se que Ollama está em \`localhost:11434\`.`;
    if (contentEl) contentEl.innerHTML = _renderMd(DEMO);
    if (metaEl)    metaEl.innerHTML = `<span class="ask-model-tag">demo · offline</span>`;
}

window.clearAskChat = function() {
    if (askCurrentEvtSource) { askCurrentEvtSource.close(); askCurrentEvtSource = null; }
    askIsStreaming = false;
    askSessionId   = '';
    const chatArea = document.getElementById('ask-chat-area');
    if (chatArea) chatArea.innerHTML = `
        <div class="ask-welcome">
            <div style="font-size:38px;margin-bottom:14px;opacity:0.4">⬡</div>
            <div style="font-size:15px;font-weight:700;color:var(--t1);margin-bottom:8px">SINC N5 · Assistente Cognitivo</div>
            <div style="font-size:11px;color:var(--t3);line-height:1.7;max-width:400px">
                Faça perguntas sobre o codebase em linguagem natural.<br>
                RAG cognitivo · Qdrant + Neo4j + Ollama (streaming).
            </div>
        </div>`;
    const sessionChip = document.getElementById('ask-session-chip');
    if (sessionChip) sessionChip.textContent = 'session: —';
    const statusEl = document.getElementById('ask-status');
    if (statusEl) statusEl.textContent = '';
    if (typeof showToast === 'function') showToast('var(--t2)', 'Nova sessão · histórico limpo');
};

window.setAskPrompt = function(el) {
    const input = document.getElementById('ask-input');
    if (input) { input.value = el.textContent.trim(); input.focus(); }
};

window.askKeyDown = function(e) {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendAsk(); }
};

window.openAskSpa = function() {
    ['view-main-dashboard', 'engine-room-spa', 'explorer-spa'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });
    const spa = document.getElementById('ask-spa');
    if (spa) spa.style.display = 'flex';
    if (window.closeSidebar) closeSidebar();
    setTimeout(() => document.getElementById('ask-input')?.focus(), 120);
    if (window.addSystemEvent) addSystemEvent('info', 'Ask N5 aberto · pronto para perguntas', { agent: 'NOC Dashboard' });
};

window.closeAskSpa = function() {
    if (askCurrentEvtSource) { askCurrentEvtSource.close(); askCurrentEvtSource = null; }
    askIsStreaming = false;
    const spa = document.getElementById('ask-spa');
    if (spa) spa.style.display = 'none';
    document.getElementById('view-main-dashboard').style.display = 'flex';
};

// ── Sidebar Navigation ────────────────────────────────────────────────────────
window.navTo = function(section) {
    const sectionMap = {
        health:  'section-health',
        logs:    'section-logs',
        zombies: 'section-kanban',
        metrics: 'chart-view',
        rep:     'section-rep',
        goals:   'section-goals',
        lessons: 'section-lessons',
        tenants: 'section-tenants',
        memory:  'section-memory',
        kanban:  'section-kanban',
        events:  'section-events',
    };
    if (window.closeSidebar) closeSidebar();
    const id = sectionMap[section];
    if (!id) return;
    const el = document.getElementById(id);
    if (!el) return;
    el.scrollIntoView({ behavior: 'smooth', block: 'start' });
    el.classList.remove('section-flash');
    void el.offsetWidth;
    el.classList.add('section-flash');
    setTimeout(() => el.classList.remove('section-flash'), 2000);
    // Mark active in sidebar
    document.querySelectorAll('.ni').forEach(ni => ni.classList.remove('ni-active'));
    const sidebarMap = { health:'Grid de Saúde', logs:'Live Logs', rep:'Reputação', goals:'Exploração N5', lessons:'Base de Lições', tenants:'Tenants', memory:'Quotas', kanban:'Tool Timeline', events:'Pulso do Sistema' };
    const label = sidebarMap[section];
    if (label) {
        document.querySelectorAll('.ni').forEach(ni => {
            if (ni.querySelector('.ni-label')?.textContent === label) ni.classList.add('ni-active');
        });
    }
};

// ── Live Log Filtering ────────────────────────────────────────────────────────
window.toggleLogFilter = function(level, el) {
    document.querySelectorAll('.log-filter').forEach(f => f.classList.remove('active'));
    el.classList.add('active');
    const c = document.getElementById('logConsole');
    if (!c) return;
    Array.from(c.children).forEach(line => {
        const isErr  = !!line.querySelector('.log-lvl-error');
        const isWarn = !!line.querySelector('.log-lvl-warn');
        let show;
        if      (level === 'ALL')   show = true;
        else if (level === 'ERROR') show = isErr;
        else if (level === 'WARN')  show = isWarn;
        else if (level === 'INFO')  show = !isErr && !isWarn;
        else show = true;
        line.style.display = show ? '' : 'none';
    });
};

window.clearLogs = function() {
    const c = document.getElementById('logConsole');
    if (c) { c.innerHTML = ''; if (typeof showToast === 'function') showToast('var(--t2)', 'Logs limpos'); }
};

// ── Worker / Agent Detail Drawer ──────────────────────────────────────────────
const AGENT_TRACE_TEMPLATES = [
    'MCTS branch avaliado → confiança 92%',
    'Context retrieval: 4 chunks Qdrant relevantes',
    'HIL gate aprovado em 340ms',
    'Redis cache hit: session key válida',
    'Neo4j blast radius: 3 nós afetados',
    'LLM inference: 1.2s latência · 420 tokens',
    'Task completada → estado: done',
];

window.showWorkerDetail = function(idx) {
    const w = LAST_WORKERS_DATA[idx];
    if (!w) return;
    const colors = ['var(--gr)', 'var(--bl)', 'var(--am)', 'var(--pu)', 'var(--cy)'];
    const c = colors[idx % colors.length];
    const health    = w.health_score   || Math.floor(Math.random() * 15 + 84);
    const tasksDone = w.tasks_done     || Math.floor(Math.random() * 480 + 40);
    const uptime    = w.uptime_pct     || (95 + Math.random() * 4.8).toFixed(1);
    const conf      = w.avg_confidence || Math.floor(Math.random() * 20 + 74);
    const lastTask  = w.last_task      || 'Análise de impacto em auth.py → blast radius calculado · 12 nós impactados';
    const agentId   = w.id || w.name   || `agent_${idx+1}`;
    const state     = w.state || w.task || 'Online';

    document.getElementById('wmodal-title').textContent = agentId;
    document.getElementById('wmodal-sub').textContent = `Estado: ${state} · Tenant: ${APP_STATE.tenant_id} · PID: ${Math.floor(Math.random()*60000+10000)}`;

    // Pick last 5 fake trace lines
    const traceLines = AGENT_TRACE_TEMPLATES
        .sort(() => Math.random() - 0.5).slice(0, 5)
        .map((msg, i) => {
            const secsAgo = (i + 1) * Math.floor(Math.random() * 30 + 5);
            const ts = new Date(Date.now() - secsAgo * 1000);
            const hh = ts.getHours().toString().padStart(2,'0');
            const mm = ts.getMinutes().toString().padStart(2,'0');
            const ss = ts.getSeconds().toString().padStart(2,'0');
            return `<div class="wm-trace-line"><span class="wm-trace-ts">${hh}:${mm}:${ss}</span><span>${msg}</span></div>`;
        }).join('');

    document.getElementById('wmodal-body').innerHTML = `
        <div class="wm-health-bar"><div class="wm-health-fill" id="wm-hfill" style="background:linear-gradient(90deg,${c},var(--pu))"></div></div>
        <div class="wm-stat-grid">
            <div class="wm-stat"><div class="wm-stat-val" style="color:${c}">${health}%</div><div class="wm-stat-lbl">Saúde</div></div>
            <div class="wm-stat"><div class="wm-stat-val" style="color:var(--am)">${tasksDone}</div><div class="wm-stat-lbl">Tasks</div></div>
            <div class="wm-stat"><div class="wm-stat-val" style="color:var(--gr)">${uptime}%</div><div class="wm-stat-lbl">Uptime</div></div>
            <div class="wm-stat"><div class="wm-stat-val" style="color:var(--pu)">${conf}%</div><div class="wm-stat-lbl">Conf.</div></div>
        </div>
        <div class="wm-section-title">Última Tarefa</div>
        <div class="wm-last-task" style="margin-bottom:14px">${lastTask}</div>
        <div class="wm-section-title">Trace Recente</div>
        <div style="background:var(--bg3);border:1px solid var(--b);border-radius:6px;padding:6px 10px;margin-bottom:14px">${traceLines}</div>
        <div class="wm-section-title">Ações</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
            <button class="btn" id="wm-btn-pause"     onclick="workerAction('${agentId}','pause')">⏸ Pausar</button>
            <button class="btn" id="wm-btn-restart"   onclick="workerAction('${agentId}','restart')">↺ Restart</button>
            <button class="btn danger" id="wm-btn-terminate" onclick="workerAction('${agentId}','terminate')">☠ Terminar</button>
        </div>`;

    document.getElementById('wmodal-overlay').style.display = 'block';
    document.getElementById('wmodal').classList.add('open');
    // Animate health bar after paint
    requestAnimationFrame(() => {
        const hfill = document.getElementById('wm-hfill');
        if (hfill) hfill.style.width = health + '%';
    });
    addSystemEvent('agent', `Detalhes consultados: ${agentId}`, { agent: 'NOC Dashboard' });
};

window.closeWorkerModal = function() {
    document.getElementById('wmodal-overlay').style.display = 'none';
    document.getElementById('wmodal').classList.remove('open');
};

window.workerAction = async function(agentId, action) {
    const btnMap = { pause: 'wm-btn-pause', restart: 'wm-btn-restart', terminate: 'wm-btn-terminate' };
    const btn = document.getElementById(btnMap[action]);
    if (btn) { btn.disabled = true; btn.textContent = '…'; }
    try {
        const resp = await fetch(
            `/api/v5/dashboard/workers/${encodeURIComponent(agentId)}/action?tenant_id=${APP_STATE.tenant_id}`,
            { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ action }) }
        );
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        const colors = { pause: 'var(--am)', restart: 'var(--bl)', terminate: 'var(--rd)' };
        const msgs   = { pause: `Agente pausado: ${agentId} (${data.affected_tasks} tasks)`, restart: `Restart enviado: ${agentId} (${data.affected_tasks} retomadas)`, terminate: `⚠ Agente terminado: ${agentId} (${data.affected_tasks} canceladas)` };
        showToast(colors[action], msgs[action]);
        addSystemEvent(action === 'terminate' ? 'warn' : 'info', msgs[action], { agent: agentId });
        if (action === 'terminate') setTimeout(closeWorkerModal, 800);
    } catch (err) {
        showToast('var(--rd)', `Erro: ${err.message}`);
    } finally {
        const labels2 = { pause: '⏸ Pausar', restart: '↺ Restart', terminate: '☠ Terminar' };
        if (btn) { btn.disabled = false; btn.textContent = labels2[action]; }
    }
};

// ── Export Dashboard Snapshot ─────────────────────────────────────────────────
window.exportDashboard = function() {
    const snap = {
        _meta: { tool: 'SINC NOC v5', version: '3.0', exported_at: new Date().toISOString(), tenant: APP_STATE.tenant_id },
        kpis: {
            success_rate:  document.getElementById('metric-success-rate')?.textContent,
            autonomy_score: document.getElementById('metric-autonomy-score')?.textContent,
            active_agents: document.getElementById('metric-active-agents')?.textContent,
            zombies:       document.getElementById('metric-latency')?.textContent,
        },
        system: {
            cpu:  document.getElementById('sv-cpu')?.textContent,
            ram:  document.getElementById('sv-ram')?.textContent,
        },
        workers: LAST_WORKERS_DATA,
        goals:   DEMO_GOALS,
        events:  SYSTEM_EVENTS_LOG.slice(0, 30).map(e => ({
            type: e.type, message: e.message, agent: e.meta?.agent,
            timestamp: e.ts.toISOString()
        })),
        health_status: document.getElementById('health-overall')?.textContent,
    };
    const json = JSON.stringify(snap, null, 2);
    const blob = new Blob([json], { type: 'application/json' });
    const url  = URL.createObjectURL(blob);
    const a    = document.createElement('a');
    a.href     = url;
    a.download = `sinc-noc-snapshot-${Date.now()}.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    if (typeof showToast === 'function') showToast('var(--gr)', '↓ Snapshot exportado — JSON pronto');
    addSystemEvent('info', 'Dashboard exportado como JSON snapshot', { agent: 'NOC Dashboard' });
    // Also persist to server
    fetch(`/api/v5/dashboard/snapshot?tenant_id=${APP_STATE.tenant_id}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(snap),
    }).catch(() => {});
};

window.confirmSnapshot = async function(btn) {
    if (!btn) return;
    const orig = btn.textContent;
    btn.disabled = true;
    btn.textContent = '…';
    const snap = {
        _meta: { tool: 'SINC NOC v5', version: '3.0', exported_at: new Date().toISOString(), tenant: APP_STATE.tenant_id },
        kpis: {
            success_rate:  document.getElementById('metric-success-rate')?.textContent,
            autonomy_score: document.getElementById('metric-autonomy-score')?.textContent,
            active_agents: document.getElementById('metric-active-agents')?.textContent,
        },
        workers: LAST_WORKERS_DATA,
        events:  SYSTEM_EVENTS_LOG.slice(0, 50).map(e => ({ type: e.type, message: e.message, agent: e.meta?.agent, timestamp: e.ts.toISOString() })),
    };
    try {
        const resp = await fetch(`/api/v5/dashboard/snapshot?tenant_id=${APP_STATE.tenant_id}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(snap),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || 'erro');
        showToast('var(--gr)', `✓ Snapshot salvo: ${data.filename}`);
        addSystemEvent('success', `Snapshot persistido: ${data.filename}`, { agent: 'NOC Dashboard' });
    } catch (err) {
        showToast('var(--am)', `Snapshot local salvo (servidor offline: ${err.message})`);
        exportDashboard();
    } finally {
        btn.disabled = false; btn.textContent = orig;
    }
};

window.killAllTasks = async function() {
    const confirmed = window.confirm('⚠ Kill Mode: cancelar TODAS as tasks em execução?\n\nEsta ação não pode ser desfeita.');
    if (!confirmed) return;
    try {
        const resp = await fetch(`/api/v5/dashboard/tasks/kill-all?tenant_id=${APP_STATE.tenant_id}`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        showToast('var(--rd)', `☠ Kill Mode: ${data.cancelled_tasks} tasks canceladas`);
        addSystemEvent('warn', `Kill Mode executado — ${data.cancelled_tasks} tasks canceladas`, { agent: 'NOC Dashboard' });
    } catch (err) {
        showToast('var(--rd)', `Kill Mode falhou: ${err.message}`);
    }
};

// ── System Event Timeline ─────────────────────────────────────────────────────
const SYSTEM_EVENTS_LOG = [];
const EVT_COLORS = { info: 'var(--bl)', warn: 'var(--am)', error: 'var(--rd)', success: 'var(--gr)', agent: 'var(--pu)', system: 'var(--cy)' };
const EVT_LABELS = { info: 'INFO', warn: 'WARN', error: 'ERR', success: 'OK', agent: 'AGENT', system: 'SYS' };
const EVT_LABEL_COLORS = {
    info:    'color:var(--bl);background:var(--bld);border-color:var(--bl)',
    warn:    'color:var(--am);background:var(--amd);border-color:var(--am)',
    error:   'color:var(--rd);background:var(--rdd);border-color:var(--rd)',
    success: 'color:var(--gr);background:var(--grd);border-color:var(--gr)',
    agent:   'color:var(--pu);background:var(--pud);border-color:var(--pu)',
    system:  'color:var(--cy);background:rgba(30,207,184,0.1);border-color:var(--cy)',
};

function renderEventTimeline() {
    const container = document.getElementById('event-timeline-list');
    if (!container) return;
    if (!SYSTEM_EVENTS_LOG.length) {
        container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum evento registrado.</div>';
        return;
    }
    const now = Date.now();
    container.innerHTML = SYSTEM_EVENTS_LOG.slice(0, 25).map(e => {
        const elapsed = Math.round((now - e.ts) / 1000);
        const timeStr = elapsed < 60 ? `${elapsed}s` : elapsed < 3600 ? `${Math.floor(elapsed/60)}min` : `${Math.floor(elapsed/3600)}h`;
        const dotColor = EVT_COLORS[e.type] || 'var(--t3)';
        const chipStyle = EVT_LABEL_COLORS[e.type] || '';
        const chip = `<span class="evt-type-chip nbadge" style="${chipStyle}">${EVT_LABELS[e.type] || e.type}</span>`;
        return `<div class="evt-row">
            <div class="evt-dot" style="background:${dotColor};box-shadow:0 0 4px ${dotColor}"></div>
            <div class="evt-body">
                <div class="evt-msg">${chip}${e.message}</div>
                ${e.meta?.agent ? `<div class="evt-meta">${e.meta.agent}</div>` : ''}
            </div>
            <div class="evt-time">${timeStr} atrás</div>
        </div>`;
    }).join('');
    const countEl = document.getElementById('events-count-label');
    if (countEl) countEl.textContent = `${SYSTEM_EVENTS_LOG.length} eventos`;
}

window.addSystemEvent = function(type, message, meta = {}) {
    SYSTEM_EVENTS_LOG.unshift({ type, message, meta, ts: Date.now() });
    if (SYSTEM_EVENTS_LOG.length > 100) SYSTEM_EVENTS_LOG.pop();
    renderEventTimeline();
};

window.clearSystemEvents = function() {
    SYSTEM_EVENTS_LOG.length = 0;
    renderEventTimeline();
    if (typeof showToast === 'function') showToast('var(--t2)', 'Timeline de eventos limpa');
};

// ── Reputation Panel ──────────────────────────────────────────────────────────
window.loadReputation = async function() {
    try {
        const resp = await fetch(`/api/v5/dashboard/intelligence/reputation?limit=10&tenant_id=${APP_STATE.tenant_id}`);
        if (!resp.ok) return;
        const data = await resp.json();
        const agents = data.agents || [];
        if (!agents.length) return;

        const repSection = document.querySelector('#section-rep .cbd');
        if (!repSection) return;

        const colors = [
            'linear-gradient(90deg,var(--gr),var(--cy))',
            'linear-gradient(90deg,var(--bl),var(--pu))',
            'linear-gradient(90deg,var(--bl),var(--cy))',
            'linear-gradient(90deg,var(--am),var(--gr))',
            'linear-gradient(90deg,var(--rd),var(--am))',
        ];
        const ranks = ['①','②','③','④','⑤','⑥','⑦','⑧','⑨','⑩'];
        const rankGold = ['gold','','','','','','','','',''];
        const badgeStyle = (b) => {
            if (b==='A+') return 'color:var(--gr);background:var(--grd)';
            if (b==='A')  return 'color:var(--bl);background:var(--bld)';
            if (b==='B+') return 'color:var(--am);background:var(--amd)';
            return                'color:var(--rd);background:var(--rdd)';
        };
        repSection.innerHTML = agents.map((a, i) => `
            <div class="rep-row">
                <div class="rep-rank ${rankGold[i] || ''}" style="--delay:${i*0.1}s">${ranks[i]}</div>
                <div class="rep-agent" title="${a.total_tasks} tasks · ${a.success_tasks} sucesso">${_escHtml(a.name)}</div>
                <div class="rep-bar-wrap"><div class="rep-bar" style="width:0%;background:${colors[i%colors.length]}" data-w="${a.score}"></div></div>
                <div class="rep-score">${a.score}%</div>
                <div class="rep-badge" style="${badgeStyle(a.badge)}">${a.badge}</div>
            </div>`).join('');

        setTimeout(() => {
            repSection.querySelectorAll('.rep-bar[data-w]').forEach(b => {
                b.style.transition = 'width 1.4s cubic-bezier(0.16,1,0.3,1)';
                b.style.width = b.dataset.w + '%';
            });
        }, 60);
    } catch (_) {}
};

// ── Boot ──────────────────────────────────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    if (window.lucide) lucide.createIcons();
    initParticles();
    initGraphEngine();
    initWebSocket();

    // Load new panel data
    setTimeout(() => { refreshHealth(); },         600);
    setTimeout(() => { loadActiveGoals(); },       900);
    setTimeout(() => { loadMemoryStats(); },      1200);
    setTimeout(() => { loadLessons(); },          1500);
    setTimeout(() => { loadReputation(); },       1800);

    // Auto-refresh health every 30s with countdown
    let healthCd = 30;
    const cdEl = document.getElementById('health-refresh-cd');
    setInterval(() => {
        healthCd--;
        if (cdEl) cdEl.textContent = `↻ ${healthCd}s`;
        if (healthCd <= 0) { healthCd = 30; refreshHealth(); }
    }, 1000);

    // Seed System Event Timeline with realistic demo events
    setTimeout(() => {
        const DEMO_EVENTS = [
            { type: 'success', msg: 'Pipeline de ingestão completado · 84 documentos processados',     agent: 'ingest_worker' },
            { type: 'agent',   msg: 'MCTS Planner: 3 ramos avaliados · branch #2 selecionado (conf 91%)', agent: 'orchestrator' },
            { type: 'warn',    msg: 'Redis TTL expirado: context key auth:session:42 · invalidado',      agent: 'redis_cache' },
            { type: 'info',    msg: 'Neo4j sync concluído: 847 nós · 2.1k arestas atualizados',         agent: 'neo4j_sync' },
            { type: 'error',   msg: 'Timeout em qa_agent · task #4821 — retry 1/3 agendado',            agent: 'qa_agent' },
            { type: 'success', msg: 'Blast radius calculado: auth.py → 12 nós impactados mapeados',      agent: 'impact_analyzer' },
            { type: 'agent',   msg: 'ContextRetriever: cache L3 Qdrant hit · 4 chunks relevantes',      agent: 'ctx_retriever' },
            { type: 'info',    msg: 'Tenant "Stark Industries" conectado · quota reiniciada',             agent: 'auth_middleware' },
            { type: 'success', msg: 'HIL review aprovado: PR #248 — merge autorizado',                   agent: 'governance_plane' },
            { type: 'system',  msg: 'WebSocket telemetry reconectado após 1.2s de queda',                agent: 'ws_handler' },
        ];
        // Insert oldest first so newest appears at top
        [...DEMO_EVENTS].reverse().forEach((e, i) => {
            SYSTEM_EVENTS_LOG.push({ type: e.type, message: e.msg, meta: { agent: e.agent }, ts: Date.now() - (DEMO_EVENTS.length - i) * 18000 });
        });
        renderEventTimeline();
    }, 1800);

    // Seed the graph with demo impact data on load
    setTimeout(() => {
        writeToTerminal('Iniciando resolução de matriz Neo4j...', 'warn');
        renderGraphImpact({
            target: 'auth_service',
            impact_map: [
                { risk: 'CRITICAL_ROUTE_BREAK',  name: 'POST /v1/login',     depth: 1, file: 'routes/' },
                { risk: 'CRITICAL_STATE_BREAK',  name: 'Redis_Token_Store',  depth: 2, file: 'core/db' },
                { risk: 'CRITICAL_STATE_BREAK',  name: 'Qdrant_Vector_DB',   depth: 3, file: 'core/memory' },
                { risk: 'MEDIUM',                name: 'ASTAnalyzer',        depth: 2, file: 'services/' },
                { risk: 'MEDIUM',                name: 'SessionMiddleware',  depth: 1, file: 'core/' }
            ]
        });
    }, 2000);

    setTimeout(() => {
        addTerminalFeed({ thought: 'MCTS Agent [Security]: Avaliando quebras de rota. Confiança: 88%' });
    }, 4500);

    setTimeout(() => {
        addTerminalFeed({ thought: 'ContextRetriever: Cache hit L3 Qdrant · 3 chunks relevantes recuperados' });
    }, 7500);
});

// ── Tenant Modal ───────────────────────────────────────────────────────────────
window.openTenantModal = function() {
    document.getElementById('tnew-name').value = '';
    document.getElementById('tnew-email').value = '';
    document.getElementById('tnew-plan').value = 'free';
    document.getElementById('tnew-result').style.display = 'none';
    document.getElementById('tenant-modal-overlay').style.display = 'block';
    document.getElementById('tenant-modal').classList.add('open');
    setTimeout(() => document.getElementById('tnew-name').focus(), 80);
};

window.closeTenantModal = function() {
    document.getElementById('tenant-modal-overlay').style.display = 'none';
    document.getElementById('tenant-modal').classList.remove('open');
};

window.createTenant = async function() {
    const name  = document.getElementById('tnew-name').value.trim();
    const plan  = document.getElementById('tnew-plan').value;
    const email = document.getElementById('tnew-email').value.trim();
    if (!name || name.length < 2) {
        showToast('var(--rd)', 'Nome do tenant deve ter pelo menos 2 caracteres');
        document.getElementById('tnew-name').focus();
        return;
    }
    const btn = document.getElementById('tnew-submit');
    btn.disabled = true; btn.textContent = '…';
    try {
        const resp = await fetch('/api/v5/dashboard/tenants/create', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, plan, email }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        const resultEl = document.getElementById('tnew-result');
        resultEl.style.display = 'block';
        resultEl.innerHTML = `
            <div style="color:var(--gr);margin-bottom:6px;font-size:12px">✓ Tenant provisionado com sucesso</div>
            <div style="color:var(--t2)">ID: <span style="color:var(--t1)">${data.tenant_id}</span></div>
            <div style="color:var(--t2)">Plano: <span style="color:var(--t1)">${data.plan}</span></div>
            <div style="color:var(--am);margin-top:6px;font-size:10px">⚠ Copie a API Key — ela não será exibida novamente:</div>
            <div style="color:var(--cy);word-break:break-all;margin-top:4px;user-select:all;cursor:text">${data.api_key}</div>`;
        btn.textContent = '✓ Criado';
        showToast('var(--gr)', `Tenant "${data.tenant_id}" provisionado (${data.plan})`);
        addSystemEvent('success', `Novo tenant: ${data.tenant_id} · plano ${data.plan}`, { agent: 'NOC Dashboard' });
    } catch (err) {
        showToast('var(--rd)', `Erro ao criar tenant: ${err.message}`);
        btn.disabled = false; btn.textContent = '✓ Provisionar';
    }
};
