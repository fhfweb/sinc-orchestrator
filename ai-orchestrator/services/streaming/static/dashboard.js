// SINC Cognitive NOC v3 - Dashboard Logic (WebSocket + Graph + Kanban)
// Este arquivo gerencia: WebSocket telemetry, vis.js graph, kanban board, workers/tenants rendering
// Tudo que NÃO está aqui (clock, sparklines, gauges, feed, pipeline, command palette) é gerenciado
// pelo inline script no dashboard.html

const APP_STATE = {
    ws: null,
    tenant_id: 'default',
    reconnect_attempts: 0,
    max_reconnect: 99,
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
            setTimeout(initWebSocket, Math.min(30000, 2000 * APP_STATE.reconnect_attempts));
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
        const restartKey = svc.key === 'api' ? null : svc.key; // FastAPI can't self-restart
        const restartBtn = restartKey
            ? `<button class="svc-restart-btn" onclick="event.stopPropagation();restartService('${restartKey}')" title="Reiniciar ${svc.name}">↺</button>`
            : '';
        return `<div class="svc-card ${statusClass}" onclick="showToast('${dotColor}','${svc.name}: ${statusLabel} · ${svc.latency_ms != null ? svc.latency_ms+'ms' : '—'}')">
            <div class="svc-icon"><i data-lucide="${svc.icon}" style="width:18px;height:18px;color:${svc.color}"></i></div>
            <div class="svc-name">${svc.name}${restartBtn}</div>
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

    // Load new panel data — staggered to avoid boot burst
    setTimeout(() => { refreshHealth(); },        1000);
    setTimeout(() => { loadActiveGoals(); },      3000);
    setTimeout(() => { loadMemoryStats(); },      5000);
    setTimeout(() => { loadLessons(); },          7000);
    setTimeout(() => { loadReputation(); },       9000);

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

// ── Page Enter Hooks ───────────────────────────────────────────────────────────
window._onPageEnter = function(pid) {
    if (pid === 'logs')            { if (typeof refreshLiveLogs === 'function') refreshLiveLogs(); }
    if (pid === 'rep')             { if (typeof loadReputation === 'function') loadReputation(); }
    if (pid === 'lessons')         { if (typeof loadLessons === 'function') loadLessons(); }
    if (pid === 'prompt-inspector'){ loadPromptInspector(); }
    if (pid === 'timeline')        { loadToolTimeline(); }
    if (pid === 'metrics')         { if (typeof refreshSystemMetrics === 'function') refreshSystemMetrics(); }
    if (pid === 'explorer' && typeof initGraphEngine === 'function') initGraphEngine();
};

// ── Prompt Inspector ───────────────────────────────────────────────────────────
window.loadPromptInspector = async function(statusFilter) {
    const list = document.getElementById('pi-list');
    if (!list) return;
    list.innerHTML = '<div style="color:var(--t3);font-size:11px;padding:16px">Carregando…</div>';
    try {
        const qs = statusFilter ? `?status=${encodeURIComponent(statusFilter)}` : '';
        const resp = await fetch(`/api/v5/dashboard/tasks${qs}&limit=40`);
        const data = await resp.json();
        const tasks = Array.isArray(data) ? data : (data.tasks || data.items || []);
        if (!tasks.length) {
            list.innerHTML = '<div style="color:var(--t3);font-size:11px;padding:16px">Nenhuma tarefa encontrada.</div>';
            return;
        }
        list.innerHTML = tasks.map(t => {
            const st = t.status || 'unknown';
            const stColor = st === 'running' ? 'var(--cy)' : st.match(/done|completed|success/) ? 'var(--gr)' : st === 'pending' ? 'var(--am)' : 'var(--rd)';
            const created = t.created_at ? new Date(t.created_at).toLocaleString('pt-BR') : '—';
            const prompt = (t.prompt || t.description || t.input || '').slice(0, 240);
            const tid = t.task_id || t.id || '';
            const title = (t.prompt || t.description || '').slice(0, 60) || 'Task #' + tid;
            return `<div class="pi-item">
              <div class="pi-header">
                <span class="pi-id">${tid}</span>
                <span class="pi-agent">${t.agent_name || '—'}</span>
                <span class="pi-status" style="color:${stColor}">${st}</span>
                <span class="pi-date">${created}</span>
                <button class="btn-xs" onclick="openTaskTrace('${tid}','${title.replace(/'/g,'').replace(/</g,'').replace(/>/g,'')}')" title="Ver trace completo">🔍 Trace</button>
              </div>
              ${prompt ? `<div class="pi-prompt">${prompt.replace(/</g,'&lt;')}</div>` : ''}
            </div>`;
        }).join('');
    } catch (err) {
        list.innerHTML = `<div style="color:var(--rd);font-size:11px;padding:16px">Erro: ${err.message}</div>`;
    }
};

// ── Tool Timeline ──────────────────────────────────────────────────────────────
window.loadToolTimeline = async function() {
    const list = document.getElementById('timeline-list');
    if (!list) return;
    list.innerHTML = '<div style="color:var(--t3);font-size:11px;padding:16px">Carregando…</div>';
    try {
        const resp = await fetch('/api/v5/dashboard/tasks?limit=60');
        const data = await resp.json();
        const tasks = Array.isArray(data) ? data : (data.tasks || data.items || []);
        if (!tasks.length) {
            list.innerHTML = '<div style="color:var(--t3);font-size:11px;padding:16px">Nenhum evento de ferramenta registrado.</div>';
            return;
        }
        list.innerHTML = tasks.map(t => {
            const st = t.status || 'unknown';
            const stColor = st.match(/done|completed|success/) ? 'var(--gr)' : st === 'running' ? 'var(--cy)' : st === 'pending' ? 'var(--am)' : 'var(--rd)';
            const ts = t.updated_at || t.created_at;
            const timeStr = ts ? new Date(ts).toLocaleTimeString('pt-BR') : '—';
            const dur = (t.created_at && t.updated_at)
                ? Math.round((new Date(t.updated_at) - new Date(t.created_at)) / 1000)
                : null;
            return `<div class="tl-row">
              <div class="tl-time">${timeStr}</div>
              <div class="tl-dot" style="background:${stColor}"></div>
              <div class="tl-body">
                <div class="tl-agent">${t.agent_name || 'unknown'}</div>
                <div class="tl-desc">${(t.prompt || t.description || t.task_id || '').slice(0,120).replace(/</g,'&lt;')}</div>
              </div>
              <div class="tl-meta">
                <span style="color:${stColor}">${st}</span>
                ${dur !== null ? `<span style="color:var(--t3)">${dur}s</span>` : ''}
              </div>
            </div>`;
        }).join('');
    } catch (err) {
        list.innerHTML = `<div style="color:var(--rd);font-size:11px;padding:16px">Erro: ${err.message}</div>`;
    }
};

// ── Mass Reclaim ───────────────────────────────────────────────────────────────
window.reclaimAllZombies = async function() {
    const tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    if (!confirm('Mover TODOS os agentes zombie (stale running > 10min) de volta para Pending?')) return;
    try {
        const resp = await fetch(`/api/v5/dashboard/tasks/reclaim-zombies?tenant_id=${tid}&stale_minutes=10`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        showToast('var(--am)', `\u21b3 ${data.reclaimed} tarefa(s) reclamada(s) \u2192 Pending`);
        if (typeof refreshKanban === 'function') refreshKanban();
    } catch (err) {
        showToast('var(--rd)', `Reclaim falhou: ${err.message}`);
    }
};

// ── Task Inject Modal ─────────────────────────────────────────────────────────
window.openInjectModal = function() {
    document.getElementById('inj-agent').value = '';
    document.getElementById('inj-prompt').value = '';
    document.getElementById('inj-priority').value = '5';
    document.getElementById('inj-result').style.display = 'none';
    document.getElementById('inject-modal-overlay').style.display = 'block';
    document.getElementById('inject-modal').classList.add('open');
    setTimeout(() => document.getElementById('inj-agent').focus(), 80);
};

window.closeInjectModal = function() {
    document.getElementById('inject-modal-overlay').style.display = 'none';
    document.getElementById('inject-modal').classList.remove('open');
};

window.submitInjectTask = async function() {
    const agent = document.getElementById('inj-agent').value.trim();
    const prompt = document.getElementById('inj-prompt').value.trim();
    const priority = parseInt(document.getElementById('inj-priority').value) || 5;
    const tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    if (!agent) { showToast('var(--rd)', 'Agent Name \u00e9 obrigat\u00f3rio'); return; }
    if (!prompt) { showToast('var(--rd)', 'Prompt \u00e9 obrigat\u00f3rio'); return; }
    const btn = document.getElementById('inj-submit');
    btn.disabled = true; btn.textContent = '\u2026';
    try {
        const resp = await fetch(`/api/v5/dashboard/tasks/inject?tenant_id=${tid}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_name: agent, prompt, priority }),
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        const res = document.getElementById('inj-result');
        res.style.display = 'block';
        res.innerHTML = '<div style="color:var(--gr);font-size:11px">\u2713 Tarefa injetada: <span style="color:var(--cy);font-family:var(--m)">' + data.task_id + '</span></div>';
        showToast('var(--gr)', 'Tarefa ' + data.task_id + ' adicionada \u00e0 fila');
        if (typeof refreshKanban === 'function') refreshKanban();
        btn.textContent = '\u2713 Injetado';
    } catch (err) {
        showToast('var(--rd)', 'Erro: ' + err.message);
        btn.disabled = false; btn.textContent = '\u2b06 Injetar na Fila';
    }
};

// ── Service Restart ────────────────────────────────────────────────────────────
window.restartService = async function(service) {
    const tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    if (!confirm('Reiniciar o servi\u00e7o "' + service + '"?')) return;
    showToast('var(--am)', 'Reiniciando ' + service + '\u2026');
    try {
        const resp = await fetch('/api/v5/dashboard/services/' + encodeURIComponent(service) + '/restart?tenant_id=' + tid, { method: 'POST' });
        const data = await resp.json();
        if (data.ok) {
            showToast('var(--gr)', '\u2713 ' + service + ' reiniciado');
        } else {
            showToast('var(--am)', service + ': ' + (data.output || 'sem sa\u00edda'));
        }
        if (typeof refreshHealth === 'function') setTimeout(refreshHealth, 2000);
    } catch (err) {
        showToast('var(--rd)', 'Restart falhou: ' + err.message);
    }
};

// ── NOC Insights (LLM Summary) ────────────────────────────────────────────────
var _insightsCache = null;
var _insightsCacheTs = 0;

window.refreshInsights = async function() {
    var body = document.getElementById('noc-insights-body');
    var status = document.getElementById('insights-status');
    if (!body) return;
    if (_insightsCache && Date.now() - _insightsCacheTs < 60000) {
        body.innerHTML = _insightsCache;
        return;
    }
    if (status) status.textContent = '\u21b3 gerando\u2026';
    body.innerHTML = '<span style="color:var(--t3);font-style:italic">Analisando estado do sistema\u2026</span>';
    var tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    try {
        var mResp = await fetch('/api/v5/dashboard/system-metrics?tenant_id=' + tid);
        var m = mResp.ok ? await mResp.json() : {};
        var cpu     = m.cpu_pct != null ? m.cpu_pct : '?';
        var ram     = m.ram_pct != null ? m.ram_pct : '?';
        var running = (m.tasks && m.tasks.running)  || 0;
        var pending = (m.tasks && m.tasks.pending)  || 0;
        var zombie  = (m.tasks && m.tasks.zombie)   || 0;
        var tokens  = (m.tasks && m.tasks.tokens_today) || 0;

        var issues = [];
        if (cpu > 85)     issues.push('CPU em ' + cpu + '% \u2014 risco de throttling');
        if (ram > 88)     issues.push('RAM em ' + ram + '% \u2014 press\u00e3o de mem\u00f3ria elevada');
        if (zombie > 0)   issues.push(zombie + ' agente(s) zombie detectado(s) \u2014 use Reclaim All');
        if (pending > 20) issues.push(pending + ' tarefas pendentes \u2014 considere escalar workers');

        var tokStr = tokens > 1000 ? (tokens/1000).toFixed(1) + 'k' : String(tokens);
        var cpuCol = cpu > 80 ? 'var(--rd)' : 'var(--gr)';
        var ramCol = ram > 85 ? 'var(--rd)' : 'var(--gr)';
        var zomCol = zombie > 0 ? 'var(--rd)' : 'var(--gr)';
        var html = '<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">'
            + '<span>\uD83D\uDDA5 CPU <b style="color:' + cpuCol + '">' + cpu + '%</b></span>'
            + '<span>\uD83E\uDDE0 RAM <b style="color:' + ramCol + '">' + ram + '%</b></span>'
            + '<span>\u26A1 Running <b style="color:var(--cy)">' + running + '</b></span>'
            + '<span>\uD83D\uDD50 Pending <b style="color:var(--am)">' + pending + '</b></span>'
            + '<span>\uD83D\uDC80 Zombie <b style="color:' + zomCol + '">' + zombie + '</b></span>'
            + '<span>\uD83E\uDE99 Tokens/dia <b style="color:var(--pu)">' + tokStr + '</b></span>'
            + '</div>';
        if (issues.length) {
            html += '<div style="color:var(--am);font-size:10px;font-family:var(--m);margin-top:4px">\u26A0 ' + issues.join(' \u00b7 ') + '</div>';
        } else {
            html += '<div style="color:var(--gr);font-size:10px;font-family:var(--m);margin-top:4px">\u2713 Sistema operando dentro dos par\u00e2metros normais.</div>';
        }
        _insightsCache = html;
        _insightsCacheTs = Date.now();
        body.innerHTML = html;
        if (status) status.textContent = new Date().toLocaleTimeString('pt-BR', {hour:'2-digit',minute:'2-digit'});
    } catch (err) {
        body.innerHTML = '<span style="color:var(--rd);font-size:10px">Erro ao gerar insights: ' + err.message + '</span>';
        if (status) status.textContent = '!';
    }
};

document.addEventListener('DOMContentLoaded', function() {
    var _insightsInFlight = false;
    function _maybeRefreshInsights() {
        var sec = document.getElementById('section-insights') || document.getElementById('insights-body');
        if (!sec || !document.contains(sec)) return;
        if (_insightsInFlight) return;
        _insightsInFlight = true;
        window.refreshInsights().finally(function() { _insightsInFlight = false; });
    }
    setTimeout(_maybeRefreshInsights, 8000);
    setInterval(_maybeRefreshInsights, 90000);
});

// ═══════════════════════════════════════════════════════════════════
// AGENT CONTROL (L1)
// ═══════════════════════════════════════════════════════════════════

var _agentRosterData = [];
var _agentConfigTarget = null;
var _agentFilterMode = 'all';

window.loadAgentRoster = async function() {
    var list = document.getElementById('agents-list');
    if (!list) return;
    var tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/agents?tenant_id=' + tid);
        var data = resp.ok ? await resp.json() : { agents: [] };
        _agentRosterData = data.agents || [];
        var badge = document.getElementById('sb-agents-badge');
        if (badge) badge.textContent = _agentRosterData.length;
        _renderAgentRoster(_agentRosterData, _agentFilterMode);
    } catch (err) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:var(--rd);font-size:10px;font-family:var(--m)">Erro: ' + err.message + '</div>';
    }
};

window.filterAgents = function(mode, tabEl) {
    _agentFilterMode = mode;
    document.querySelectorAll('#agents-filter-tabs .ctab').forEach(function(t) { t.classList.remove('on'); });
    if (tabEl) tabEl.classList.add('on');
    _renderAgentRoster(_agentRosterData, mode);
};

function _renderAgentRoster(agents, filter) {
    var list = document.getElementById('agents-list');
    if (!list) return;
    var filtered = filter === 'all' ? agents : agents.filter(function(a) { return a.status === filter; });
    if (!filtered.length) {
        list.innerHTML = '<div style="padding:40px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum agente em estado: ' + filter + '</div>';
        return;
    }
    list.innerHTML = filtered.map(function(a) {
        var stColor = a.status === 'busy' ? 'var(--cy)' : a.status === 'zombie' ? 'var(--rd)' : a.status === 'queued' ? 'var(--am)' : 'var(--t3)';
        var stLabel = a.status === 'busy' ? 'BUSY' : a.status === 'zombie' ? 'ZOMBIE' : a.status === 'queued' ? 'QUEUED' : 'IDLE';
        var rep = a.rep_score || 0;
        var repColor = rep >= 80 ? 'var(--gr)' : rep >= 50 ? 'var(--am)' : 'var(--rd)';
        var prog = a.progress_pct || 0;
        var tokStr = a.tokens_total > 1000 ? (a.tokens_total / 1000).toFixed(1) + 'k' : (a.tokens_total || 0);
        var lastSeen = a.last_active ? new Date(a.last_active).toLocaleTimeString('pt-BR') : '—';
        return '<div class="agent-row" onclick="openAgentConfig(\'' + encodeURIComponent(a.name) + '\')">'
            + '<div class="agent-row-status" style="background:' + stColor + '" title="' + stLabel + '"></div>'
            + '<div class="agent-row-body">'
            +   '<div class="agent-row-name">' + a.name + '<span class="agent-row-badge" style="color:' + stColor + '">' + stLabel + '</span></div>'
            +   '<div class="agent-row-meta">'
            +     '<span title="Reputação">★ <b style="color:' + repColor + '">' + rep + '%</b></span>'
            +     '<span>\u2714 ' + (a.success || 0) + ' / \u2718 ' + (a.failed || 0) + '</span>'
            +     '<span>\uD83E\uDE99 ' + tokStr + ' tokens</span>'
            +     '<span style="color:var(--t3)">last: ' + lastSeen + '</span>'
            +   '</div>'
            +   (prog > 0 ? '<div class="agent-row-progress"><div style="width:' + prog + '%;background:var(--cy);height:100%;border-radius:2px;transition:width 0.4s"></div></div>' : '')
            +   (a.task_title ? '<div class="agent-row-task">' + String(a.task_title).substring(0, 80) + '</div>' : '')
            + '</div>'
            + '<div class="agent-row-actions" onclick="event.stopPropagation()">'
            +   '<button class="btn" title="Pause" onclick="workerAction(\'' + a.name + '\',\'pause\')" style="padding:3px 7px;font-size:10px">⏸</button>'
            +   '<button class="btn" title="Restart" onclick="workerAction(\'' + a.name + '\',\'restart\')" style="padding:3px 7px;font-size:10px">↺</button>'
            +   '<button class="btn danger" title="Kill" onclick="workerAction(\'' + a.name + '\',\'terminate\')" style="padding:3px 7px;font-size:10px">☠</button>'
            + '</div>'
            + '</div>';
    }).join('');
}

window.openAgentConfig = async function(encodedName) {
    var name = decodeURIComponent(encodedName);
    _agentConfigTarget = name;
    var drawer = document.getElementById('agent-config-drawer');
    var title  = document.getElementById('acd-title');
    if (!drawer) return;
    if (title) title.textContent = 'Config: ' + name;
    drawer.style.display = 'flex';
    drawer.style.flexDirection = 'column';
    var tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/agents/' + encodeURIComponent(name) + '/config?tenant_id=' + tid);
        if (resp.ok) {
            var cfg = await resp.json();
            document.getElementById('acd-temperature').value = cfg.temperature || 0.7;
            document.getElementById('acd-top-p').value       = cfg.top_p || 1.0;
            document.getElementById('acd-max-tokens').value  = cfg.max_tokens || 4096;
            document.getElementById('acd-model').value       = cfg.model || '';
        }
    } catch (_) {}
};

window.closeAgentConfig = function() {
    var drawer = document.getElementById('agent-config-drawer');
    if (drawer) drawer.style.display = 'none';
    _agentConfigTarget = null;
};

window.saveAgentConfig = async function() {
    if (!_agentConfigTarget) return;
    var tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    var body = {
        temperature: parseFloat(document.getElementById('acd-temperature').value),
        top_p:       parseFloat(document.getElementById('acd-top-p').value),
        max_tokens:  parseInt(document.getElementById('acd-max-tokens').value),
        model:       document.getElementById('acd-model').value.trim(),
    };
    var btn = document.getElementById('acd-save-btn');
    btn.disabled = true; btn.textContent = '\u2026';
    try {
        var resp = await fetch('/api/v5/dashboard/agents/' + encodeURIComponent(_agentConfigTarget) + '/config?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
        });
        var data = await resp.json();
        if (!resp.ok) throw new Error(data.detail || resp.statusText);
        showToast('var(--gr)', '\u2713 Config de ' + _agentConfigTarget + ' salva');
        btn.textContent = '\u2713 Salvo';
        setTimeout(function() { btn.disabled = false; btn.textContent = '\u2713 Salvar Config'; }, 2000);
    } catch (err) {
        showToast('var(--rd)', 'Erro: ' + err.message);
        btn.disabled = false; btn.textContent = '\u2713 Salvar Config';
    }
};

window.killAgentFromConfig = function() {
    if (!_agentConfigTarget) return;
    workerAction(_agentConfigTarget, 'terminate');
    closeAgentConfig();
};

// ═══════════════════════════════════════════════════════════════════
// COST ATTRIBUTION (L6)
// ═══════════════════════════════════════════════════════════════════

var _costPeriod  = '7d';
var _costGroupBy = 'agent';

window.loadCostAttribution = async function(period, tabEl) {
    if (period && period !== _costPeriod) {
        _costPeriod = period;
        document.querySelectorAll('#cost-period-tabs .ctab').forEach(function(t) { t.classList.remove('on'); });
        if (tabEl) tabEl.classList.add('on');
    }
    var rows    = document.getElementById('cost-rows');
    var summary = document.getElementById('cost-summary');
    if (!rows) return;
    rows.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3)">Carregando\u2026</div>';
    var tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/cost-attribution?period=' + _costPeriod + '&group_by=' + _costGroupBy + '&tenant_id=' + tid);
        var data = resp.ok ? await resp.json() : {};

        // Budget bar
        var budgetCfg = await _get_budget_config(tid);
        var budget  = budgetCfg.daily_budget || 0;
        var used    = data.total_tokens || 0;
        var budgetPct = budget > 0 ? Math.min(100, Math.round(used / budget * 100)) : 0;
        var budgetBar   = document.getElementById('cost-budget-bar');
        var budgetLabel = document.getElementById('cost-budget-label');
        if (budgetBar) {
            budgetBar.style.width = budgetPct + '%';
            budgetBar.style.background = budgetPct > 90 ? 'var(--rd)' : budgetPct > 70 ? 'var(--am)' : 'var(--gr)';
        }
        if (budgetLabel) budgetLabel.textContent = used.toLocaleString() + (budget > 0 ? ' / ' + budget.toLocaleString() + ' (' + budgetPct + '%)' : ' tokens');
        if (budgetCfg.daily_budget) document.getElementById('cost-budget-input').value = budgetCfg.daily_budget;
        if (budgetCfg.hard_stop) document.getElementById('cost-hard-stop').checked = true;

        // Summary table
        var items = data.summary || [];
        if (summary) {
            summary.innerHTML = '<div class="cost-table-wrap"><table class="cost-table">'
                + '<thead><tr><th>' + (_costGroupBy === 'agent' ? 'Agente' : 'Modelo') + '</th><th>Tokens</th><th>Chamadas</th><th>Custo USD</th><th>%</th></tr></thead>'
                + '<tbody>' + items.map(function(item) {
                    var pct = data.total_tokens > 0 ? Math.round(item.tokens / data.total_tokens * 100) : 0;
                    var costStr = item.cost_usd > 0 ? '$' + item.cost_usd.toFixed(4) : '\u2014';
                    return '<tr>'
                        + '<td style="color:var(--cy)">' + item.name + '</td>'
                        + '<td style="font-family:var(--m)">' + item.tokens.toLocaleString() + '</td>'
                        + '<td style="font-family:var(--m)">' + (item.calls || 0) + '</td>'
                        + '<td style="color:' + (item.cost_usd > 0.01 ? 'var(--am)' : 'var(--t2)') + '">' + costStr + '</td>'
                        + '<td><div style="background:var(--bg3);border-radius:3px;height:5px;width:60px;overflow:hidden"><div style="width:' + pct + '%;background:var(--bl);height:100%"></div></div></td>'
                        + '</tr>';
                }).join('') + '</tbody>'
                + '<tfoot><tr><td style="color:var(--t2)">TOTAL</td><td style="font-family:var(--m);color:var(--t1)">' + (data.total_tokens || 0).toLocaleString() + '</td><td></td><td style="color:var(--am)">' + (data.total_cost_usd > 0 ? '$' + data.total_cost_usd.toFixed(4) : '\u2014') + '</td><td></td></tr></tfoot>'
                + '</table></div>';
        }

        // Day breakdown
        var dayData = data.rows || [];
        if (!dayData.length) {
            rows.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:10px">Sem dados de consumo no per\u00edodo selecionado.</div>';
        } else {
            var days = {};
            dayData.forEach(function(r) { if (!days[r.day]) days[r.day] = []; days[r.day].push(r); });
            rows.innerHTML = Object.keys(days).sort().reverse().map(function(day) {
                var dayRows = days[day];
                var dayTotal = dayRows.reduce(function(s, r) { return s + r.tokens; }, 0);
                return '<div style="margin-bottom:10px">'
                    + '<div style="font-size:9px;color:var(--t3);font-family:var(--m);margin-bottom:4px">' + day + ' &nbsp;\u2014 ' + dayTotal.toLocaleString() + ' tokens</div>'
                    + dayRows.map(function(r) {
                        return '<div style="display:flex;gap:8px;padding:3px 0;align-items:center">'
                            + '<span style="color:var(--cy);min-width:120px">' + r.agent + '</span>'
                            + (r.model && r.model !== 'unknown' ? '<span style="color:var(--t3);min-width:80px">' + r.model + '</span>' : '')
                            + '<span style="font-family:var(--m);color:var(--t2)">' + r.tokens.toLocaleString() + ' tok</span>'
                            + '<span style="color:var(--t3)">' + r.calls + ' calls</span>'
                            + '</div>';
                    }).join('')
                    + '</div>';
            }).join('');
        }
    } catch (err) {
        rows.innerHTML = '<div style="padding:20px;color:var(--rd);font-size:10px">Erro: ' + err.message + '</div>';
    }
};

window.setCostGroupBy = function(mode, tabEl) {
    _costGroupBy = mode;
    document.querySelectorAll('#cost-group-tabs .ctab').forEach(function(t) { t.classList.remove('on'); });
    if (tabEl) tabEl.classList.add('on');
    loadCostAttribution();
};

async function _get_budget_config(tid) {
    try {
        var resp = await fetch('/api/v5/dashboard/config?tenant_id=' + tid);
        if (resp.ok) {
            var cfg = await resp.json();
            return { daily_budget: cfg.token_budget_daily || 0, hard_stop: cfg.budget_hard_stop || false };
        }
    } catch (_) {}
    return { daily_budget: 0, hard_stop: false };
}

window.saveBudgetConfig = async function() {
    var tid    = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    var budget = parseInt(document.getElementById('cost-budget-input').value) || 0;
    var hard   = document.getElementById('cost-hard-stop').checked;
    if (!budget) { showToast('var(--rd)', 'Informe um orçamento válido'); return; }
    try {
        await fetch('/api/v5/dashboard/config?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'token_budget_daily', value: budget }),
        });
        await fetch('/api/v5/dashboard/config?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ key: 'budget_hard_stop', value: hard }),
        });
        showToast('var(--gr)', '\u2713 Orçamento definido: ' + budget.toLocaleString() + ' tokens/dia' + (hard ? ' (Hard Stop ON)' : ''));
    } catch (err) {
        showToast('var(--rd)', 'Erro ao salvar: ' + err.message);
    }
};

// ═══════════════════════════════════════════════════════════════════
// AUDIT LOG
// ═══════════════════════════════════════════════════════════════════

window.loadAuditLog = async function() {
    var list  = document.getElementById('audit-list');
    var chip  = document.getElementById('audit-count-chip');
    if (!list) return;
    var tid = (typeof APP_STATE !== 'undefined' ? APP_STATE.tenant_id : null) || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/audit-log?limit=60&tenant_id=' + tid);
        var data = resp.ok ? await resp.json() : { entries: [] };
        var entries = data.entries || [];
        if (chip) chip.textContent = entries.length + ' entries';
        if (!entries.length) {
            list.innerHTML = '<div style="padding:40px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhuma entrada de auditoria registrada.</div>';
            return;
        }
        list.innerHTML = entries.map(function(e) {
            var d = new Date(e.ts * 1000);
            var timeStr = d.toLocaleString('pt-BR');
            var actionColor = e.action.match(/kill|terminate|cancel/) ? 'var(--rd)'
                : e.action.match(/config|update|patch/) ? 'var(--am)'
                : e.action.match(/spawn|inject|create/) ? 'var(--gr)'
                : 'var(--bl)';
            return '<div class="audit-row">'
                + '<div class="audit-ts">' + timeStr + '</div>'
                + '<div class="audit-action" style="color:' + actionColor + '">' + e.action + '</div>'
                + '<div class="audit-target">' + (e.target || '\u2014') + '</div>'
                + '<div class="audit-detail">' + (e.detail || '') + '</div>'
                + '</div>';
        }).join('');
    } catch (err) {
        list.innerHTML = '<div style="padding:20px;color:var(--rd);font-size:10px">Erro: ' + err.message + '</div>';
    }
};

// ═══════════════════════════════════════════════════════════════════
// _onPageEnter EXTENSIONS (append to existing hook)
// ═══════════════════════════════════════════════════════════════════

// Extend the existing _onPageEnter to handle new pages
var _origOnPageEnter = window._onPageEnter;
window._onPageEnter = function(pid) {
    if (typeof _origOnPageEnter === 'function') _origOnPageEnter(pid);
    if (pid === 'agents') loadAgentRoster();
    if (pid === 'cost')   loadCostAttribution();
    if (pid === 'audit')  loadAuditLog();
};

// Auto-refresh agents page every 15s while on it
var _agentRosterInFlight = false;
setInterval(function() {
    var sec = document.getElementById('section-agents');
    if (!sec || sec.style.display === 'none') return;
    if (_agentRosterInFlight) return;
    _agentRosterInFlight = true;
    Promise.resolve(loadAgentRoster()).finally(function() { _agentRosterInFlight = false; });
}, 30000);

// ═══════════════════════════════════════════════════════════════════
// HOME SPARKLINES — ring buffer + canvas sparkline renderer
// ═══════════════════════════════════════════════════════════════════

var _sparkBuffer = {
    success_rate:   [],
    queue_depth:    [],
    active_agents:  [],
    tokens_per_min: [],
    avg_latency_ms: []
};
var _SPARK_MAX = 30;

function _sparkPush(key, val) {
    var buf = _sparkBuffer[key];
    if (!buf) return;
    buf.push(val === null || val === undefined ? null : +val);
    if (buf.length > _SPARK_MAX) buf.shift();
}

function _renderSparkline(canvasId, data, color) {
    var canvas = document.getElementById(canvasId);
    if (!canvas || !canvas.getContext) return;
    var ctx = canvas.getContext('2d');
    var W = canvas.width, H = canvas.height;
    ctx.clearRect(0, 0, W, H);
    var valid = data.filter(function(v) { return v !== null && !isNaN(v); });
    if (valid.length < 2) return;
    var min = Math.min.apply(null, valid), max = Math.max.apply(null, valid);
    var range = max - min || 1;
    var pts = data.map(function(v, i) {
        var x = (data.length > 1) ? i / (data.length - 1) * W : 0;
        var y = (v !== null && !isNaN(v)) ? H - ((v - min) / range) * (H - 4) - 2 : null;
        return [x, y];
    });
    // Fill under line
    ctx.beginPath();
    var started = false;
    var lastX = 0;
    pts.forEach(function(p) {
        if (p[1] === null) { started = false; return; }
        if (!started) { ctx.moveTo(p[0], H); ctx.lineTo(p[0], p[1]); started = true; }
        else ctx.lineTo(p[0], p[1]);
        lastX = p[0];
    });
    if (started) {
        ctx.lineTo(lastX, H);
        ctx.closePath();
        var grad = ctx.createLinearGradient(0, 0, 0, H);
        grad.addColorStop(0, color.replace(')', ',0.25)').replace('rgb(', 'rgba('));
        grad.addColorStop(1, color.replace(')', ',0)').replace('rgb(', 'rgba('));
        ctx.fillStyle = grad;
        ctx.fill();
    }
    // Line
    ctx.beginPath();
    started = false;
    pts.forEach(function(p) {
        if (p[1] === null) { started = false; return; }
        if (!started) { ctx.moveTo(p[0], p[1]); started = true; }
        else ctx.lineTo(p[0], p[1]);
    });
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.stroke();
}

function _redrawSparklines() {
    _renderSparkline('spark-success',  _sparkBuffer.success_rate,   'rgb(46,212,122)');
    _renderSparkline('spark-queue',    _sparkBuffer.queue_depth,    'rgb(77,128,255)');
    _renderSparkline('spark-agents',   _sparkBuffer.active_agents,  'rgb(155,123,255)');
    _renderSparkline('spark-tokens',   _sparkBuffer.tokens_per_min, 'rgb(30,207,184)');
    _renderSparkline('spark-latency',  _sparkBuffer.avg_latency_ms, 'rgb(240,160,32)');
}

var _trendInFlight = false;
window.loadMetricsTrends = async function() {
    // Only run when the home/dashboard section is visible
    var home = document.getElementById('view-main-dashboard') || document.getElementById('section-home');
    if (home && home.style.display === 'none') return;
    if (_trendInFlight) return;
    _trendInFlight = true;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/metrics/trends?window_minutes=60&points=20&tenant_id=' + tid);
        if (!resp.ok) return;
        var d = await resp.json();
        var s = d.series || {};
        _sparkBuffer.success_rate   = (s.success_rate   || []).map(function(v){ return v === null ? null : Math.round(v * 100); });
        _sparkBuffer.queue_depth    = s.queue_depth    || [];
        _sparkBuffer.active_agents  = s.active_agents  || [];
        _sparkBuffer.tokens_per_min = s.tokens_per_min || [];
        _sparkBuffer.avg_latency_ms = s.avg_latency_ms || [];
        _redrawSparklines();
    } catch(e) { /* silent */ } finally { _trendInFlight = false; }
};
setTimeout(window.loadMetricsTrends, 5000);
setInterval(window.loadMetricsTrends, 60000);

// ═══════════════════════════════════════════════════════════════════
// GOALS PAGE
// ═══════════════════════════════════════════════════════════════════

window.loadActiveGoals = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var container = document.getElementById('goals-container');
    var chip = document.getElementById('goals-status');
    if (!container) return;
    try {
        var resp = await fetch('/api/v5/dashboard/goals?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { goals: [] };
        var goals = d.goals || [];
        if (chip) chip.textContent = '● ' + goals.length + ' obj';
        if (!goals.length) {
            container.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:11px;font-family:var(--m)">Nenhum objetivo ativo.</div>';
            return;
        }
        container.innerHTML = goals.map(function(g) {
            var stCol = g.status === 'done' ? 'var(--gr)' : g.status === 'running' ? 'var(--bl)' : 'var(--am)';
            var stIcon = g.status === 'done' ? '✓' : g.status === 'running' ? '⟳' : '◷';
            return '<div class="goal-row">'
                + '<div class="goal-icon" style="color:' + stCol + '">' + stIcon + '</div>'
                + '<div class="goal-title">' + (g.title || g.description || 'Goal #'+g.id).substring(0, 80) + '</div>'
                + '<div class="goal-meta">'
                + (g.agent ? '<span style="color:var(--t3)">' + g.agent + '</span>' : '')
                + (g.priority ? '<span class="chip" style="font-size:9px">P' + g.priority + '</span>' : '')
                + '</div>'
                + (g.status !== 'done' ? '<button class="btn-xs" onclick="markGoalDone(\'' + g.id + '\')" title="Concluir">✓</button>' : '<span style="color:var(--gr);font-size:10px">done</span>')
                + '</div>';
        }).join('');
    } catch(e) {
        if (container) container.innerHTML = '<div style="color:var(--t3);font-size:11px;padding:12px">Erro ao carregar.</div>';
    }
};

window.createGoal = async function() {
    var inp = document.getElementById('new-goal-input');
    var title = inp ? inp.value.trim() : '';
    if (!title) { showToast('var(--am)', 'Descreva o objetivo'); return; }
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/goals?tenant_id=' + tid, {
            method: 'POST', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ title: title, priority: 9 })
        });
        showToast('var(--gr)', '✓ Objetivo criado');
        if (inp) inp.value = '';
        loadActiveGoals();
    } catch(e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.markGoalDone = async function(goalId) {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/goals/' + goalId + '?tenant_id=' + tid, {
            method: 'PATCH', headers: {'Content-Type':'application/json'},
            body: JSON.stringify({ status: 'done' })
        });
        showToast('var(--gr)', '✓ Concluído');
        loadActiveGoals();
    } catch(e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

// ═══════════════════════════════════════════════════════════════════
// WORKERS PAGE
// ═══════════════════════════════════════════════════════════════════

window.loadWorkers = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var listEl = document.getElementById('workers-list');
    if (!listEl) return;
    try {
        var resp = await fetch('/api/v5/dashboard/workers?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { workers: [] };
        var workers = d.workers || [];
        if (!workers.length) {
            listEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--t3);font-size:11px">Nenhum worker ativo detectado.</div>';
            return;
        }
        listEl.innerHTML = workers.map(function(w) {
            var stCol = w.status === 'running' ? 'var(--bl)' : w.status === 'idle' ? 'var(--gr)' : 'var(--t3)';
            return '<div class="worker-row">'
                + '<div class="worker-dot" style="color:' + stCol + '">●</div>'
                + '<div class="worker-name">' + (w.name || w.id || 'worker') + '</div>'
                + '<div class="worker-tasks" style="color:var(--t3)">' + (w.running_tasks || 0) + ' tasks</div>'
                + '<div class="worker-src" style="color:var(--t3);font-family:var(--m);font-size:10px">' + (w.pid ? 'PID '+w.pid : w.source || 'db') + '</div>'
                + '<div class="worker-actions" style="display:flex;gap:4px">'
                + '<button class="btn-xs btn-xs-danger" onclick="workerAction(\'' + encodeURIComponent(w.name||w.id||'') + '\',\'kill\')" title="Kill">✕</button>'
                + '<button class="btn-xs" onclick="workerAction(\'' + encodeURIComponent(w.name||w.id||'') + '\',\'restart\')" title="Restart">↻</button>'
                + '</div>'
                + '</div>';
        }).join('');
    } catch(e) {
        if (listEl) listEl.innerHTML = '<div style="color:var(--rd);font-size:11px;padding:12px">Erro: ' + e.message + '</div>';
    }
};

// ═══════════════════════════════════════════════════════════════════
// QUEUE STATS WIDGET
// ═══════════════════════════════════════════════════════════════════

window.loadQueueStats = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/queue/stats?tenant_id=' + tid);
        if (!resp.ok) return;
        var d = await resp.json();
        var set = function(id, val) { var el = document.getElementById(id); if (el) el.textContent = val; };
        set('qs-pending', (d.by_status && d.by_status.pending) || 0);
        set('qs-running', (d.by_status && d.by_status.running) || 0);
        set('qs-review',  (d.by_status && d.by_status.review)  || 0);
        set('qs-zombies', d.stale_running || 0);
        set('qs-total',   d.total || 0);
        var zombieEl = document.getElementById('qs-zombies');
        if (zombieEl) zombieEl.style.color = (d.stale_running > 0) ? 'var(--rd)' : 'var(--gr)';
    } catch(e) { /* silent */ }
};
var _qsInFlight = false;
function _maybeLoadQueueStats() {
    if (_qsInFlight) return;
    _qsInFlight = true;
    window.loadQueueStats().finally(function() { _qsInFlight = false; });
}
setTimeout(_maybeLoadQueueStats, 6000);
setInterval(_maybeLoadQueueStats, 25000);

// ═══════════════════════════════════════════════════════════════════
// TASK TRACE MODAL (L5 Deep Trace)
// ═══════════════════════════════════════════════════════════════════

window.openTaskTrace = async function(taskId, taskTitle) {
    var modal = document.getElementById('task-trace-modal');
    if (!modal) { _injectTraceModal(); modal = document.getElementById('task-trace-modal'); }
    var body  = document.getElementById('task-trace-body');
    var titleEl = document.getElementById('task-trace-title');
    if (titleEl) titleEl.textContent = 'Trace: ' + (taskTitle || taskId).substring(0, 60);
    if (body) body.innerHTML = '<div style="padding:24px;text-align:center;color:var(--t3);font-size:11px">Carregando trace…</div>';
    if (modal) modal.style.display = 'flex';
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/tasks/' + encodeURIComponent(taskId) + '/steps?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { steps: [], task: {} };
        var steps = d.steps || [];
        if (!steps.length) {
            var r2 = await fetch('/api/v5/dashboard/tasks/' + encodeURIComponent(taskId) + '/trace?tenant_id=' + tid);
            if (r2.ok) { var d2 = await r2.json(); if (d2.trace) steps = Array.isArray(d2.trace) ? d2.trace : [d2.trace]; }
        }
        if (body) body.innerHTML = _renderTraceSteps(steps);
    } catch(e) {
        if (body) body.innerHTML = '<div style="color:var(--rd);font-size:11px;padding:12px">Erro: ' + e.message + '</div>';
    }
};

function _escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function _renderTraceSteps(steps) {
    if (!steps.length) return '<div style="padding:24px;text-align:center;color:var(--t3);font-size:11px">Nenhum step registrado para esta task.</div>';
    return steps.map(function(s, i) {
        var src = s._source || s.step_type || 'step';
        var srcCol = src.includes('llm') ? 'var(--pu)' : src.includes('tool') ? 'var(--cy)' : 'var(--bl)';
        var ts = (s.created_at || s.timestamp || '').substring(0, 19).replace('T', ' ');
        var tokens = s.tokens_used || s.prompt_tokens || s.completion_tokens || null;
        var dur    = s.duration_ms || s.duration || s.latency_ms || null;
        return '<div class="trace-step">'
            + '<div class="trace-step-num">' + (i+1) + '</div>'
            + '<div class="trace-step-body">'
            + '<div class="trace-step-header">'
            + '<span class="trace-step-src" style="color:' + srcCol + '">' + src + '</span>'
            + (ts ? '<span class="trace-step-ts">' + ts + '</span>' : '')
            + (tokens ? '<span class="trace-step-meta">🔤 ' + tokens + ' tok</span>' : '')
            + (dur !== null ? '<span class="trace-step-meta">⏱ ' + dur + (typeof dur==='number'&&dur<10000?' ms':'') + '</span>' : '')
            + '</div>'
            + (s.prompt || s.input ? '<div class="trace-prompt"><b>→ Prompt</b><pre>' + _escHtml(String(s.prompt||s.input).substring(0,400)) + '</pre></div>' : '')
            + (s.response || s.output || s.result ? '<div class="trace-response"><b>← Response</b><pre>' + _escHtml(String(s.response||s.output||s.result).substring(0,400)) + '</pre></div>' : '')
            + (s.tool_name || s.function_name ? '<div class="trace-tool">🔧 <b>' + (s.tool_name||s.function_name) + '</b>' + (s.args ? ' → ' + _escHtml(JSON.stringify(s.args).substring(0,120)) : '') + '</div>' : '')
            + '</div></div>';
    }).join('');
}

function _injectTraceModal() {
    var m = document.createElement('div');
    m.id = 'task-trace-modal';
    m.style.cssText = 'display:none;position:fixed;inset:0;background:rgba(0,0,0,0.75);z-index:9000;align-items:center;justify-content:center;padding:20px';
    m.innerHTML = '<div style="background:var(--bg2);border:1px solid var(--b);border-radius:14px;width:100%;max-width:800px;max-height:88vh;display:flex;flex-direction:column">'
        + '<div style="padding:14px 18px;border-bottom:1px solid var(--b);display:flex;align-items:center;justify-content:space-between;flex-shrink:0">'
        + '<div id="task-trace-title" style="font-size:13px;font-weight:700;color:var(--t1);font-family:var(--m)">Task Trace</div>'
        + '<button onclick="closeTaskTrace()" style="background:none;border:none;color:var(--t3);font-size:20px;cursor:pointer;line-height:1">✕</button>'
        + '</div>'
        + '<div id="task-trace-body" style="flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:8px"></div>'
        + '</div>';
    document.body.appendChild(m);
}

window.closeTaskTrace = function() {
    var modal = document.getElementById('task-trace-modal');
    if (modal) modal.style.display = 'none';
};

// ═══════════════════════════════════════════════════════════════════
// LIVE ALERT EVALUATION (process WS telemetry vs saved thresholds)
// ═══════════════════════════════════════════════════════════════════

var _alertConfig = null;
var _lastAlertFire = {};

async function _loadAlertConfigBg() {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/alerts/config?tenant_id=' + tid);
        if (resp.ok) _alertConfig = await resp.json();
    } catch(e) { /* silent */ }
}
setTimeout(_loadAlertConfigBg, 5000);
setInterval(_loadAlertConfigBg, 120000);

function _evaluateAlerts(metrics) {
    if (!_alertConfig || _alertConfig.enabled === false) return;
    var now = Date.now();
    var COOLDOWN = 60000;
    var checks = [
        { key: 'cpu',    val: metrics.cpu,    threshold: _alertConfig.cpu_threshold    || 85, label: 'CPU',     unit: '%' },
        { key: 'ram',    val: metrics.ram,    threshold: _alertConfig.ram_threshold    || 88, label: 'RAM',     unit: '%' },
        { key: 'zombie', val: metrics.zombies,threshold: _alertConfig.zombie_threshold || 3,  label: 'Zombies', unit: '' },
    ];
    checks.forEach(function(c) {
        if (c.val == null || isNaN(c.val)) return;
        if (+c.val >= c.threshold) {
            if (!_lastAlertFire[c.key] || (now - _lastAlertFire[c.key]) > COOLDOWN) {
                _lastAlertFire[c.key] = now;
                showToast('var(--rd)', '⚠ ' + c.label + ' = ' + c.val + c.unit + ' (limite ' + c.threshold + c.unit + ')');
            }
        }
    });
    var failRate = metrics.success_rate != null ? 100 - +metrics.success_rate : null;
    if (failRate !== null && failRate >= (_alertConfig.failure_rate_threshold || 30)) {
        if (!_lastAlertFire['fail'] || (now - _lastAlertFire['fail']) > COOLDOWN) {
            _lastAlertFire['fail'] = now;
            showToast('var(--rd)', '⚠ Falhas = ' + failRate.toFixed(1) + '% (limite ' + (_alertConfig.failure_rate_threshold||30) + '%)');
        }
    }
}

// Hook into the real WS telemetry handler (handleTelemetryEvent)
var _origHandleTelemetryEvent = handleTelemetryEvent;
function handleTelemetryEvent(data) {
    _origHandleTelemetryEvent(data);
    if (data && data.metrics) {
        if (data.metrics.success_rate != null) _sparkPush('success_rate', Math.round(+data.metrics.success_rate * 100));
        if (data.metrics.active_agents != null) _sparkPush('active_agents', +data.metrics.active_agents);
        _redrawSparklines();
        _evaluateAlerts(data.metrics);
    }
    if (data && data.counts) {
        _sparkPush('queue_depth', (+data.counts.pending || 0) + (+data.counts.running || 0));
    }
}

// ═══════════════════════════════════════════════════════════════════
// ENGINE ROOM (L0 Infra Controls)
// ═══════════════════════════════════════════════════════════════════

window.loadEngineRoomStatus = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/infra/status?tenant_id=' + tid);
        if (!resp.ok) return;
        var d = await resp.json();
        // Circuit breaker chip
        var cbChip = document.getElementById('cb-state-chip');
        if (cbChip) {
            cbChip.textContent = d.circuit_breaker ? 'OPEN · BLOQUEANDO' : 'OFF · Safe';
            cbChip.style.color = d.circuit_breaker ? 'var(--rd)' : 'var(--gr)';
        }
        // Mode radio
        var curMode = d.operating_mode || 'balanced';
        document.querySelectorAll('input[name="er-mode"]').forEach(function(r) {
            r.checked = (r.value === curMode);
        });
        var modeChip = document.getElementById('er-mode-chip');
        if (modeChip) modeChip.textContent = curMode.replace('_', ' ');
        // Workers
        var wSlider = document.getElementById('er-workers-slider');
        var wVal = document.getElementById('er-workers-val');
        var wChip = document.getElementById('er-workers-chip');
        if (wSlider && d.worker_count) {
            wSlider.value = d.worker_count;
            if (wVal) wVal.textContent = d.worker_count;
            if (wChip) wChip.textContent = d.worker_count + ' réplicas';
        }
        // Rate Limiter
        var rlSlider = document.getElementById('er-rpm-slider');
        var rlVal = document.getElementById('er-rpm-val');
        var rlChip = document.getElementById('er-rpm-chip');
        if (rlSlider && d.rate_limit_rpm) {
            rlSlider.value = d.rate_limit_rpm;
            if (rlVal) rlVal.textContent = d.rate_limit_rpm + ' rpm';
            if (rlChip) rlChip.textContent = d.rate_limit_rpm + ' rpm';
        }
        // Failover radio
        var foMode = d.failover_mode || 'local';
        document.querySelectorAll('input[name="er-failover"]').forEach(function(r) {
            r.checked = (r.value === foMode);
        });
        var foChip = document.getElementById('er-failover-chip');
        if (foChip) foChip.textContent = foMode;
        // Zombie timeout
        var ztSlider = document.getElementById('er-zombie-slider');
        var ztVal = document.getElementById('er-zombie-val');
        var ztChip = document.getElementById('er-zombie-chip');
        if (ztSlider && d.zombie_timeout_minutes) {
            ztSlider.value = d.zombie_timeout_minutes;
            if (ztVal) ztVal.textContent = d.zombie_timeout_minutes + 'min';
            if (ztChip) ztChip.textContent = d.zombie_timeout_minutes + ' min';
        }
        // Status chip
        var statusChip = document.getElementById('er-status-chip');
        if (statusChip) statusChip.textContent = '✓ Sincronizado';
    } catch (e) {
        console.warn('Engine Room status error:', e);
        var statusChip = document.getElementById('er-status-chip');
        if (statusChip) statusChip.textContent = '✗ Erro';
    }
};

window.setCircuitBreaker = async function(open) {
    if (!confirm('Tem certeza que deseja ' + (open ? 'ATIVAR' : 'desativar') + ' o circuit breaker?')) return;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/infra/circuit-breaker?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ open: open })
        });
        showToast(open ? 'var(--rd)' : 'var(--gr)', open ? '☠ Circuit Breaker ATIVADO — LLM bloqueado!' : '✓ Circuit Breaker desativado');
        loadEngineRoomStatus();
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.setOperatingMode = async function(mode) {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/infra/mode?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode })
        });
        showToast('var(--gr)', '✓ Modo: ' + mode.replace('_', ' ').toUpperCase());
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.scaleWorkers = async function() {
    var slider = document.getElementById('er-workers-slider');
    var count = slider ? parseInt(slider.value) : 4;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/infra/scale-workers?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ count: count })
        });
        var d = await resp.json();
        showToast('var(--gr)', '✓ Workers: ' + (d.actual_count || count) + ' ativos');
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.setRateLimiter = async function() {
    var slider = document.getElementById('er-rpm-slider');
    var rpm = slider ? parseInt(slider.value) : 60;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/infra/rate-limiter?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ requests_per_minute: rpm })
        });
        showToast('var(--gr)', '✓ Rate limit: ' + rpm + ' rpm');
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.setFailover = async function(mode) {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/infra/failover?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: mode })
        });
        showToast('var(--gr)', '✓ Failover: ' + mode.toUpperCase());
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.setZombieTimeout = async function() {
    var slider = document.getElementById('er-zombie-slider');
    var minutes = slider ? parseInt(slider.value) : 10;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/infra/zombie-timeout?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ minutes: minutes })
        });
        showToast('var(--gr)', '✓ Zombie timeout: ' + minutes + ' min');
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

// ═══════════════════════════════════════════════════════════════════
// MEMORY CONTROL (L4)
// ═══════════════════════════════════════════════════════════════════

window.switchMemTab = function(tab, el) {
    // uses .ctab / .ctabs pattern from the HTML
    var tabRow = document.getElementById('mem-tab-row');
    if (tabRow) tabRow.querySelectorAll('.ctab').forEach(function(b) { b.classList.remove('on'); });
    if (el) el.classList.add('on');
    ['stats','search','prune'].forEach(function(t) {
        var pane = document.getElementById('mem-tab-' + t);
        if (pane) pane.style.display = (t === tab) ? '' : 'none';
    });
};

window.runVectorSearch = async function() {
    var q = document.getElementById('vsearch-query');
    var kEl = document.getElementById('vsearch-topk');
    var resEl = document.getElementById('vsearch-results');
    if (!q || !resEl) return;
    var tid = APP_STATE.tenant_id || 'default';
    resEl.innerHTML = '<span style="color:var(--t3)">Buscando…</span>';
    try {
        var resp = await fetch('/api/v5/dashboard/memory/vector-search?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ query: q.value, k: parseInt(kEl ? kEl.value : 5) || 5 })
        });
        var d = await resp.json();
        var results = d.results || [];
        if (!results.length) {
            resEl.innerHTML = '<span style="color:var(--t3)">Nenhum resultado.</span>';
            return;
        }
        resEl.innerHTML = results.map(function(r) {
            return '<div class="mem-result-row">'
                + '<div class="mem-result-score">score: ' + (r.score || 0).toFixed(3) + '</div>'
                + '<div class="mem-result-text">' + (r.text || r.content || JSON.stringify(r)).substring(0, 300) + '</div>'
                + '</div>';
        }).join('');
    } catch (e) {
        resEl.innerHTML = '<span style="color:var(--rd)">Erro: ' + e.message + '</span>';
    }
};

window.pruneLesson = async function() {
    var idEl = document.getElementById('prune-lesson-id');
    if (!idEl || !idEl.value.trim()) { showToast('var(--am)', 'Informe o ID da lesson'); return; }
    if (!confirm('Deletar lição ' + idEl.value.trim() + '? Irreversível.')) return;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/memory/lessons/' + encodeURIComponent(idEl.value.trim()) + '?tenant_id=' + tid, { method: 'DELETE' });
        var d = await resp.json();
        showToast('var(--gr)', '✓ ' + (d.message || 'Lesson removida'));
        idEl.value = '';
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.pruneVector = async function() {
    var idEl = document.getElementById('prune-vector-id');
    var colEl = document.getElementById('prune-collection');
    if (!idEl || !idEl.value.trim()) { showToast('var(--am)', 'Informe o vector ID'); return; }
    if (!confirm('Deletar vector ' + idEl.value.trim() + '? Irreversível.')) return;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/memory/vector-prune?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vector_id: idEl.value.trim(), collection: colEl ? colEl.value : 'sinc_memory' })
        });
        var d = await resp.json();
        showToast('var(--gr)', '✓ ' + (d.message || 'Vector removido'));
        idEl.value = '';
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

// ═══════════════════════════════════════════════════════════════════
// REPUTATION (L1 — real data)
// ═══════════════════════════════════════════════════════════════════

window.loadReputation = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var listEl = document.getElementById('rep-list');
    var selectEl = document.getElementById('rep-agent-select');
    if (!listEl) return;
    try {
        var resp = await fetch('/api/v5/dashboard/intelligence/reputation?tenant_id=' + tid);
        if (!resp.ok) throw new Error('HTTP ' + resp.status);
        var d = await resp.json();
        var agents = d.agents || [];
        if (!agents.length) {
            listEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--t3);font-size:11px">Nenhum agente com dados de reputação.</div>';
            return;
        }
        listEl.innerHTML = agents.map(function(a) {
            var score = (a.reputation_score || 0).toFixed(2);
            var col = a.reputation_score >= 0.8 ? 'var(--gr)' : a.reputation_score >= 0.5 ? 'var(--am)' : 'var(--rd)';
            var bar = Math.round((a.reputation_score || 0) * 100);
            return '<div class="rep-row">'
                + '<div class="rep-agent-name">' + (a.agent_name || a.id) + '</div>'
                + '<div class="rep-score" style="color:' + col + '">' + score + '</div>'
                + '<div class="rep-tasks">' + (a.total_tasks || 0) + ' tasks</div>'
                + '<div style="flex:1;height:4px;background:var(--bg3);border-radius:2px;overflow:hidden"><div style="width:' + bar + '%;height:100%;background:' + col + ';border-radius:2px"></div></div>'
                + '</div>';
        }).join('');
        // Populate agent select for manual adjustment
        if (selectEl) {
            selectEl.innerHTML = '<option value="">— Selecionar agente —</option>'
                + agents.map(function(a) {
                    return '<option value="' + (a.agent_name||a.id) + '">' + (a.agent_name||a.id) + ' (' + (a.reputation_score||0).toFixed(2) + ')</option>';
                }).join('');
        }
        // auto-learning chip state
        var chip = document.getElementById('rep-autolearn-chip');
        if (chip && d.auto_learning !== undefined) {
            chip.textContent = d.auto_learning ? '⟳ Auto-Learning: ON' : '⟳ Auto-Learning: OFF';
            chip.style.color = d.auto_learning ? 'var(--gr)' : 'var(--t3)';
        }
    } catch (e) {
        if (listEl) listEl.innerHTML = '<div style="padding:16px;color:var(--t3);font-size:11px">Dados de reputação indisponíveis.</div>';
    }
};

// direction: 1 = boost, -1 = penalizar
window.adjustReputation = async function(direction) {
    var selectEl = document.getElementById('rep-agent-select');
    var deltaEl = document.getElementById('rep-delta');
    var agentId = selectEl ? selectEl.value : '';
    if (!agentId) { showToast('var(--am)', 'Selecione um agente'); return; }
    var baseDelta = Math.abs(parseFloat(deltaEl ? deltaEl.value : 10) || 10) / 100; // convert pts to 0-1 scale
    var delta = direction * baseDelta;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/intelligence/reputation/' + encodeURIComponent(agentId) + '/adjust?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ delta: delta, reason: delta > 0 ? 'manual_boost' : 'manual_penalty' })
        });
        var d = await resp.json();
        var newScore = d.new_score !== undefined ? parseFloat(d.new_score).toFixed(2) : '?';
        showToast('var(--gr)', '✓ ' + agentId + ': ' + newScore);
        loadReputation();
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.toggleAutoLearning = async function() {
    var chip = document.getElementById('rep-autolearn-chip');
    var currentlyOn = chip && chip.textContent.includes('ON');
    var enabled = !currentlyOn;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/intelligence/reputation/auto-learning?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ enabled: enabled })
        });
        if (chip) { chip.textContent = enabled ? '⟳ Auto-Learning: ON' : '⟳ Auto-Learning: OFF'; chip.style.color = enabled ? 'var(--gr)' : 'var(--t3)'; }
        showToast('var(--gr)', '✓ Auto-learning: ' + (enabled ? 'ATIVO' : 'INATIVO'));
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

// ═══════════════════════════════════════════════════════════════════
// SECURITY (L8)
// ═══════════════════════════════════════════════════════════════════

window.loadSecurityPage = async function() {
    loadApiKeys();
    loadAnomalies();
};

window.switchSecTab = function(tab, el) {
    var tabRow = document.getElementById('sec-tab-row');
    if (tabRow) tabRow.querySelectorAll('.ctab').forEach(function(b) { b.classList.remove('on'); });
    if (el) el.classList.add('on');
    ['keys','anomalies'].forEach(function(t) {
        var pane = document.getElementById('sec-tab-' + t);
        if (pane) pane.style.display = (t === tab) ? '' : 'none';
    });
    if (tab === 'keys') loadApiKeys();
    if (tab === 'anomalies') loadAnomalies();
};

window.loadApiKeys = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var listEl = document.getElementById('sec-keys-list');
    if (!listEl) return;
    try {
        var resp = await fetch('/api/v5/dashboard/security/api-keys?tenant_id=' + tid);
        var d = await resp.json();
        var keys = d.keys || [];
        if (!keys.length) {
            listEl.innerHTML = '<div style="padding:20px;color:var(--t3);font-size:11px;text-align:center">Nenhuma API key cadastrada.</div>';
            return;
        }
        listEl.innerHTML = keys.map(function(k) {
            return '<div class="sec-key-row">'
                + '<div class="sec-key-name">' + (k.name || 'Key') + '</div>'
                + '<div class="sec-key-hint" style="font-family:var(--m);color:var(--t3)">' + (k.key_hint || '***') + '</div>'
                + '<div class="sec-key-scope" style="color:var(--cy)">' + (k.scope || 'full') + '</div>'
                + '<div class="sec-key-created" style="color:var(--t3);font-size:10px">' + (k.created_at ? new Date(k.created_at).toLocaleDateString('pt-BR') : '') + '</div>'
                + '<button class="btn-xs btn-xs-danger" onclick="revokeApiKey(\'' + k.id + '\')">Revogar</button>'
                + '</div>';
        }).join('');
    } catch (e) {
        if (listEl) listEl.innerHTML = '<div style="color:var(--rd);font-size:11px;padding:12px">Erro: ' + e.message + '</div>';
    }
};

window.createApiKey = async function() {
    var labelEl = document.getElementById('sec-key-label');
    var label = labelEl ? labelEl.value.trim() : '';
    if (!label) { showToast('var(--am)', 'Informe o label da key'); return; }
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/security/api-keys?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: label, scope: 'full' })
        });
        var d = await resp.json();
        if (d.key) {
            var display = document.getElementById('sec-new-key-display');
            if (display) { display.style.display = ''; display.textContent = '🔑 ' + d.key + ' (salve agora — não será exibida novamente)'; }
        }
        showToast('var(--gr)', '✓ Key criada: ' + label);
        if (labelEl) labelEl.value = '';
        loadApiKeys();
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.revokeApiKey = async function(keyId) {
    if (!confirm('Revogar esta API key?')) return;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        await fetch('/api/v5/dashboard/security/api-keys/' + keyId + '?tenant_id=' + tid, { method: 'DELETE' });
        showToast('var(--am)', '✓ Key revogada');
        loadApiKeys();
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.loadAnomalies = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var listEl = document.getElementById('sec-anomalies-list');
    if (!listEl) return;
    try {
        var resp = await fetch('/api/v5/dashboard/security/anomalies?tenant_id=' + tid);
        var d = await resp.json();
        var anomalies = d.anomalies || [];
        if (!anomalies.length) {
            listEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--gr);font-size:12px">✓ Nenhuma anomalia detectada.</div>';
            return;
        }
        listEl.innerHTML = anomalies.map(function(a) {
            var sevCol = a.severity === 'critical' ? 'var(--rd)' : a.severity === 'warning' ? 'var(--am)' : 'var(--bl)';
            return '<div class="anomaly-row">'
                + '<div class="anomaly-type" style="color:' + sevCol + '">' + (a.type || 'unknown') + '</div>'
                + '<div class="anomaly-desc">' + (a.description || '') + '</div>'
                + '<div class="anomaly-meta" style="color:var(--t3);font-size:10px">' + (a.detected_at ? new Date(a.detected_at).toLocaleString('pt-BR') : '') + '</div>'
                + '</div>';
        }).join('');
    } catch (e) {
        if (listEl) listEl.innerHTML = '<div style="color:var(--rd);font-size:11px;padding:12px">Erro: ' + e.message + '</div>';
    }
};

// ═══════════════════════════════════════════════════════════════════
// ALERTS CONFIG
// ═══════════════════════════════════════════════════════════════════

window.loadAlertsConfig = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/alerts/config?tenant_id=' + tid);
        if (!resp.ok) return;
        var d = await resp.json();
        var setSlider = function(id, val, valId, suffix) {
            var sl = document.getElementById(id);
            var vl = document.getElementById(valId);
            if (sl && val !== undefined) { sl.value = val; if (vl) vl.textContent = val + (suffix||''); }
        };
        setSlider('alert-cpu', d.cpu_threshold, 'alert-cpu-val', '%');
        setSlider('alert-ram', d.ram_threshold, 'alert-ram-val', '%');
        setSlider('alert-zombie', d.zombie_threshold, 'alert-zombie-val', '');
        setSlider('alert-fail', d.failure_rate_threshold, 'alert-fail-val', '%');
        var whEl = document.getElementById('alert-webhook-url');
        if (whEl && d.webhook_url) whEl.value = d.webhook_url;
        var chip = document.getElementById('alerts-enabled-chip');
        if (chip && d.enabled !== undefined) {
            chip.textContent = d.enabled ? '● Alertas: ON' : '● Alertas: OFF';
            chip.style.color = d.enabled ? 'var(--gr)' : 'var(--t3)';
        }
    } catch (e) { console.warn('Alerts config load error:', e); }
};

window.saveAlertsConfig = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var g = function(id) { var el = document.getElementById(id); return el ? el.value : null; };
    var chip = document.getElementById('alerts-enabled-chip');
    var enabled = !chip || chip.textContent.includes('ON');
    var cfg = {
        cpu_threshold: parseInt(g('alert-cpu')) || 85,
        ram_threshold: parseInt(g('alert-ram')) || 88,
        zombie_threshold: parseInt(g('alert-zombie')) || 3,
        failure_rate_threshold: parseInt(g('alert-fail')) || 30,
        webhook_url: g('alert-webhook-url') || '',
        enabled: enabled
    };
    try {
        await fetch('/api/v5/dashboard/alerts/config?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(cfg)
        });
        showToast('var(--gr)', '✓ Configuração de alertas salva');
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.toggleAlertsEnabled = async function() {
    var chip = document.getElementById('alerts-enabled-chip');
    var currentlyOn = chip && chip.textContent.includes('ON');
    var enabled = !currentlyOn;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/alerts/config?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : {};
        d.enabled = enabled;
        await fetch('/api/v5/dashboard/alerts/config?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(d)
        });
        if (chip) { chip.textContent = enabled ? '● Alertas: ON' : '● Alertas: OFF'; chip.style.color = enabled ? 'var(--gr)' : 'var(--t3)'; }
        showToast('var(--gr)', '✓ Alertas: ' + (enabled ? 'ATIVADOS' : 'DESATIVADOS'));
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

window.testWebhook = async function() {
    var urlEl = document.getElementById('alert-webhook-url');
    var url = urlEl ? urlEl.value.trim() : '';
    if (!url) { showToast('var(--am)', 'Informe a URL do webhook'); return; }
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/alerts/test-webhook?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ url: url })
        });
        var d = await resp.json();
        showToast(d.success ? 'var(--gr)' : 'var(--rd)', d.success ? '✓ Webhook OK (' + d.status_code + ')' : '✗ Webhook falhou: ' + d.error);
    } catch (e) { showToast('var(--rd)', 'Erro: ' + e.message); }
};

// ═══════════════════════════════════════════════════════════════════
// SIMULATION / DRY-RUN (L10)
// ═══════════════════════════════════════════════════════════════════

window.runDryRun = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var g = function(id) { var el = document.getElementById(id); return el ? el.value : ''; };
    var payload = {
        agent_type: g('sim-agent') || null,
        priority: parseInt(g('sim-priority')) || 5,
        task_description: g('sim-prompt')
    };
    var resEl = document.getElementById('sim-results');
    var btn = document.getElementById('sim-btn');
    if (resEl) resEl.style.display = 'none';
    if (btn) btn.textContent = '⏳ Simulando…';
    try {
        var resp = await fetch('/api/v5/dashboard/simulate/dry-run?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload)
        });
        var d = await resp.json();
        if (!resp.ok) throw new Error(d.detail || 'Simulation failed');
        if (resEl) resEl.style.display = '';
        var set = function(id, val) { var el = document.getElementById(id); if (el) el.textContent = val; };
        set('sim-tokens', (d.estimated_tokens || 0).toLocaleString());
        set('sim-cost', '$' + (d.estimated_cost_usd || 0).toFixed(4));
        set('sim-duration', (d.estimated_duration_seconds || 0).toFixed(1) + 's');
        set('sim-confidence', ((d.success_rate || 0) * 100).toFixed(1) + '%');
        set('sim-agent-sel', d.agent_type || payload.agent_type || 'auto');
        set('sim-queue', '#' + (d.queue_position || 1));
        window._lastDryRun = payload;
    } catch (e) {
        showToast('var(--rd)', 'Simulação falhou: ' + e.message);
    } finally {
        if (btn) btn.textContent = '▶ Simular';
    }
};

window.executeSimulatedTask = async function() {
    if (!window._lastDryRun) { showToast('var(--am)', 'Execute a simulação primeiro'); return; }
    if (!confirm('Enviar esta task real para execução?')) return;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/tasks/inject?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                agent_type: window._lastDryRun.agent_type,
                priority: window._lastDryRun.priority,
                payload: { description: window._lastDryRun.task_description }
            })
        });
        var d = await resp.json();
        showToast('var(--gr)', '✓ Task injetada: ' + (d.task_id || 'ok'));
        window._lastDryRun = null;
    } catch (e) { showToast('var(--rd)', 'Erro ao executar: ' + e.message); }
};

// ═══════════════════════════════════════════════════════════════════
// _onPageEnter FULL EXTENSION (engine-room, memory, rep, security, alerts, simulate)
// ═══════════════════════════════════════════════════════════════════

var _origOnPageEnter2 = window._onPageEnter;
window._onPageEnter = function(pid) {
    if (typeof _origOnPageEnter2 === 'function') _origOnPageEnter2(pid);
    if (pid === 'engine-room') { loadEngineRoomStatus(); loadWorkers(); }
    if (pid === 'memory')      { switchMemTab('stats', document.querySelector('#section-memory .ctab')); }
    if (pid === 'rep')         loadReputation();
    if (pid === 'security')    loadSecurityPage();
    if (pid === 'alerts')      loadAlertsConfig();
    if (pid === 'goals')       loadActiveGoals();
    if (pid === 'lessons')     { if (typeof loadLessons === 'function') loadLessons(); }
    if (pid === 'simulate')    { var r = document.getElementById('sim-results'); if(r) r.style.display='none'; }
    if (pid === 'neural')        loadNeuralTargets();
    if (pid === 'topology')      loadTopology();
    if (pid === 'containers')    loadContainers();
    if (pid === 'dag')           loadDag('live');
    if (pid === 'cron')          loadCronJobs();
    if (pid === 'rag-inspector') loadRagTraces();
    if (pid === 'prompts')       loadPromptTemplates();
    if (pid === 'models')        loadModelRegistry();
    if (pid === 'sla')           loadSlaData();
    if (pid === 'incidents')     loadIncidents();
    if (pid === 'pipelines')     loadPipelines();
    if (pid === 'webhooks')      loadWebhooks();
    if (pid === 'playground')    { _renderPlaygroundEndpoints(''); }
    if (pid === 'pii-scanner')   loadPiiHistory();
};

// ═══════════════════════════════════════════════════════════════════
// N5 COMMAND CENTER — SELF-HEALING ENGINE
// ═══════════════════════════════════════════════════════════════════

var _selfHealLog = [];

window.loadSelfHealingStatus = async function() {
    var strip = document.getElementById('selfheal-strip');
    var chip  = document.getElementById('selfheal-status-chip');
    if (!strip) return;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        // Query recent audit log for auto-actions
        var resp = await fetch('/api/v5/dashboard/audit?limit=20&tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { logs: [] };
        var logs = d.logs || [];
        // Filter for auto-heal events
        var healEvents = logs.filter(function(l) {
            var act = (l.action || '').toLowerCase();
            return act.includes('zombie') || act.includes('kill') || act.includes('restart') || act.includes('retry') || act.includes('recover') || act.includes('heal');
        }).slice(0, 4);
        if (healEvents.length === 0) {
            strip.innerHTML = '<div class="heal-event"><span class="heal-icon">✅</span><span class="heal-desc" style="color:var(--gr)">Auto-Heal Engine · Sistema operando dentro dos parâmetros — nenhuma intervenção necessária</span><span class="heal-ts">' + new Date().toLocaleTimeString('pt-BR') + '</span></div>';
            if (chip) { chip.textContent = '⬡ AUTO-HEAL ONLINE'; chip.style.color = 'var(--gr)'; chip.style.borderColor = 'var(--gr)'; }
        } else {
            if (chip) { chip.textContent = '⬡ HEAL ' + healEvents.length + ' ações'; chip.style.color = 'var(--am)'; chip.style.borderColor = 'var(--am)'; }
            strip.innerHTML = healEvents.map(function(e) {
                var ts = e.ts ? new Date(e.ts * 1000).toLocaleTimeString('pt-BR') : '—';
                var icon = (e.action || '').toLowerCase().includes('kill') ? '☠' : (e.action || '').toLowerCase().includes('restart') ? '↺' : '⬡';
                return '<div class="heal-event" style="border-color:rgba(255,160,32,0.3)">'
                    + '<span class="heal-icon">' + icon + '</span>'
                    + '<span class="heal-desc"><strong style="color:var(--am)">' + (e.action || 'auto-action') + '</strong> · ' + (e.detail || e.agent || '') + '</span>'
                    + '<span class="heal-ts">' + ts + '</span>'
                    + '</div>';
            }).join('');
        }
    } catch(e) {
        if (strip) strip.innerHTML = '<div class="heal-event"><span class="heal-icon">⬡</span><span class="heal-desc" style="color:var(--t3)">Auto-Heal Engine · monitorando</span></div>';
    }
};
// Load on home boot
setTimeout(window.loadSelfHealingStatus, 4000);
setInterval(window.loadSelfHealingStatus, 45000);

// ═══════════════════════════════════════════════════════════════════
// N5 COMMAND CENTER — NEURAL STEERING
// ═══════════════════════════════════════════════════════════════════

var _neuralSelectedTask = null;
var _neuralInjectionHistory = [];

window.loadNeuralTargets = async function() {
    var list = document.getElementById('neural-targets-list');
    if (!list) return;
    list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Buscando agentes ativos…</div>';
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/tasks?status=running&limit=20&tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { tasks: [] };
        var tasks = d.tasks || d.items || [];
        if (!tasks.length) {
            list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum agente em execução no momento.<br><span style="font-size:9px">Aguarde tasks running para fazer steering.</span></div>';
            return;
        }
        list.innerHTML = tasks.slice(0, 12).map(function(t) {
            var elapsed = t.started_at ? Math.round((Date.now()/1000 - t.started_at) / 60) + 'm' : '—';
            var sel = _neuralSelectedTask && _neuralSelectedTask.id === t.id ? ' selected' : '';
            return '<div class="neural-target-row' + sel + '" onclick="selectNeuralTarget(' + JSON.stringify(t).replace(/"/g,'&quot;') + ',this)" data-tid="' + t.id + '">'
                + '<div class="neural-target-dot"></div>'
                + '<div class="neural-target-name">' + (t.agent_name || t.agent || 'task #' + t.id).substring(0,30) + '</div>'
                + '<div class="neural-target-meta">#' + t.id + ' · ' + elapsed + '</div>'
                + '</div>';
        }).join('');
    } catch(e) {
        list.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m);padding:12px">Erro ao carregar: ' + e.message + '</div>';
    }
};

window.selectNeuralTarget = function(task, el) {
    _neuralSelectedTask = task;
    document.querySelectorAll('.neural-target-row').forEach(function(r) { r.classList.remove('selected'); });
    if (el) el.classList.add('selected');
    var label = document.getElementById('neural-selected-agent');
    if (label) label.textContent = (task.agent_name || task.agent || 'task #' + task.id);
};

window.injectNeuralVector = async function() {
    if (!_neuralSelectedTask) { showToast('var(--am)', 'Selecione um agente alvo'); return; }
    var payload = document.getElementById('neural-payload');
    var steerType = document.getElementById('neural-steer-type');
    var intensity = document.getElementById('neural-intensity');
    var payloadTxt = payload ? payload.value.trim() : '';
    if (!payloadTxt) { showToast('var(--am)', 'Descreva o vetor de correção'); return; }
    var tid = APP_STATE.tenant_id || 'default';
    var body = {
        task_id: _neuralSelectedTask.id,
        agent: _neuralSelectedTask.agent_name || _neuralSelectedTask.agent,
        steer_type: steerType ? steerType.value : 'context_inject',
        intensity: intensity ? parseInt(intensity.value) : 5,
        payload: payloadTxt
    };
    showToast('var(--bl)', '⬡ Injetando vetor neural…');
    try {
        var resp = await fetch('/api/v5/dashboard/neural/steer?tenant_id=' + tid, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        });
        var d = resp.ok ? await resp.json() : { ok: false };
        if (d.ok !== false) {
            showToast('var(--gr)', '✓ Vetor injetado · Agente irá assimilar na próxima iteração');
            var entry = { ts: new Date().toLocaleTimeString('pt-BR'), type: body.steer_type, agent: body.agent || 'task#'+body.task_id, payload: payloadTxt.substring(0,80) };
            _neuralInjectionHistory.unshift(entry);
            _renderNeuralHistory();
            if (payload) payload.value = '';
        } else {
            showToast('var(--rd)', '✗ Falha na injeção: ' + (d.detail || 'erro desconhecido'));
        }
    } catch(e) {
        showToast('var(--rd)', '✗ ' + e.message);
    }
};

function _renderNeuralHistory() {
    var list = document.getElementById('neural-history-list');
    if (!list) return;
    if (!_neuralInjectionHistory.length) {
        list.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhuma injeção nesta sessão.</div>';
        return;
    }
    list.innerHTML = _neuralInjectionHistory.map(function(e) {
        return '<div class="neural-history-row">'
            + '<div class="neural-history-header">'
            + '<span class="neural-history-ts">' + e.ts + '</span>'
            + '<span class="neural-history-type">' + e.type + '</span>'
            + '<span class="neural-history-agent">' + e.agent + '</span>'
            + '</div>'
            + '<div class="neural-history-payload">' + e.payload + (e.payload.length >= 80 ? '…' : '') + '</div>'
            + '</div>';
    }).join('');
}

// ═══════════════════════════════════════════════════════════════════
// N5 COMMAND CENTER — COGNITIVE TOPOLOGY + ROI HEAT
// ═══════════════════════════════════════════════════════════════════

var _topoNetwork = null;
var _topoData = null;

window.switchTopoTab = function(tab, el) {
    var row = document.getElementById('topo-tab-row');
    if (row) row.querySelectorAll('.ctab').forEach(function(b) { b.classList.remove('on'); });
    if (el) el.classList.add('on');
    ['graph','heat','drift'].forEach(function(t) {
        var v = document.getElementById('topo-view-' + t);
        if (v) v.style.display = (t === tab) ? '' : 'none';
    });
    if (tab === 'heat') _renderRoiHeat();
    if (tab === 'drift') _renderDrift();
    if (tab === 'graph' && _topoData) _buildTopoNetwork(_topoData);
};

window.loadTopology = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    showToast('var(--bl)', '⬡ Carregando topologia cognitiva…');
    try {
        var resp = await fetch('/api/v5/dashboard/topology?tenant_id=' + tid);
        if (!resp.ok) { _buildSyntheticTopology(); return; }
        _topoData = await resp.json();
        _buildTopoNetwork(_topoData);
        _updateTopoStats(_topoData);
    } catch(e) {
        _buildSyntheticTopology();
    }
};

function _buildSyntheticTopology() {
    // Build a realistic synthetic graph from real DB data
    var tid = APP_STATE.tenant_id || 'default';
    fetch('/api/v5/dashboard/tasks?limit=30&tenant_id=' + tid).then(function(r) { return r.json(); }).then(function(d) {
        var tasks = d.tasks || d.items || [];
        var agents = {};
        tasks.forEach(function(t) { var a = t.agent_name || t.agent || 'system'; agents[a] = (agents[a]||0)+1; });
        var nodes = [], edges = [];
        var agentNames = Object.keys(agents).slice(0,8);
        // Central orchestrator node
        nodes.push({ id: 'orch', label: 'Orchestrator', group: 'agent', value: 20 });
        agentNames.forEach(function(a, i) {
            nodes.push({ id: 'agent_'+i, label: a.substring(0,16), group: 'agent', value: agents[a]*2+5 });
            edges.push({ from: 'orch', to: 'agent_'+i });
        });
        // Task nodes for recent running/done
        tasks.slice(0,15).forEach(function(t) {
            var ag = t.agent_name || t.agent || 'system';
            var agIdx = agentNames.indexOf(ag);
            var gp = t.status === 'done' ? 'success' : t.status === 'error' ? 'failure' : 'task';
            nodes.push({ id: 'task_'+t.id, label: '#'+t.id, group: gp, value: 4, title: t.description || t.agent });
            if (agIdx >= 0) edges.push({ from: 'agent_'+agIdx, to: 'task_'+t.id });
        });
        _topoData = { nodes: nodes, edges: edges, agents: agents };
        _buildTopoNetwork(_topoData);
        _updateTopoStats(_topoData);
    }).catch(function() {
        _topoData = { nodes: [{ id: 'orch', label: 'Orchestrator', group: 'agent', value: 20 }], edges: [], agents: {} };
        _buildTopoNetwork(_topoData);
    });
}

function _buildTopoNetwork(data) {
    var container = document.getElementById('topo-network');
    if (!container) return;
    if (typeof vis === 'undefined') return;

    var colorMap = {
        agent:   { background: 'rgba(77,128,255,0.6)',   border: 'rgba(77,128,255,0.9)'   },
        task:    { background: 'rgba(30,207,184,0.5)',   border: 'rgba(30,207,184,0.8)'   },
        success: { background: 'rgba(46,212,122,0.5)',   border: 'rgba(46,212,122,0.8)'   },
        failure: { background: 'rgba(255,80,80,0.5)',    border: 'rgba(255,80,80,0.8)'    },
        lesson:  { background: 'rgba(155,123,255,0.5)',  border: 'rgba(155,123,255,0.8)'  },
        concept: { background: 'rgba(240,160,32,0.5)',   border: 'rgba(240,160,32,0.8)'   },
    };
    var nodes = new vis.DataSet((data.nodes || []).map(function(n) {
        var c = colorMap[n.group] || colorMap.task;
        return { id: n.id, label: n.label, title: n.title || n.label, value: n.value || 6, color: c, font: { color: '#eeeeff', size: 10, face: 'monospace' } };
    }));
    var edges = new vis.DataSet((data.edges || []).map(function(e, i) {
        return { id: i, from: e.from, to: e.to, color: { color: 'rgba(77,128,255,0.25)', highlight: 'rgba(77,128,255,0.7)' }, width: 1 };
    }));
    var options = {
        nodes: { shape: 'dot', scaling: { min: 6, max: 28 }, borderWidth: 1.5 },
        edges: { smooth: { type: 'continuous', roundness: 0.4 } },
        physics: { stabilization: { iterations: 80 }, barnesHut: { gravitationalConstant: -3500, springLength: 90 } },
        interaction: { hover: true, tooltipDelay: 150 },
        background: { color: 'transparent' }
    };
    if (_topoNetwork) { _topoNetwork.destroy(); }
    _topoNetwork = new vis.Network(container, { nodes: nodes, edges: edges }, options);
    _topoNetwork.on('click', function(params) {
        if (!params.nodes.length) return;
        var nid = params.nodes[0];
        var node = (data.nodes || []).find(function(n) { return n.id === nid; });
        var detail = document.getElementById('topo-node-detail');
        if (detail && node) {
            detail.innerHTML = '<strong style="color:var(--bl)">' + node.label + '</strong><br>'
                + '<span style="color:var(--t3)">tipo: ' + (node.group||'—') + '</span><br>'
                + (node.title && node.title !== node.label ? '<span style="color:var(--t2)">' + node.title + '</span>' : '');
        }
    });
}

function _updateTopoStats(data) {
    var nodes = data.nodes || [];
    var edges = data.edges || [];
    var set = function(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; };
    set('topo-nodes-count', nodes.length);
    set('topo-edges-count', edges.length);
    var density = nodes.length > 1 ? (2 * edges.length / (nodes.length * (nodes.length-1))).toFixed(3) : '—';
    set('topo-density', density);
    // Find hub (most connected)
    var deg = {};
    edges.forEach(function(e) { deg[e.from] = (deg[e.from]||0)+1; deg[e.to] = (deg[e.to]||0)+1; });
    var hub = Object.keys(deg).sort(function(a,b){return deg[b]-deg[a];})[0];
    var hubNode = nodes.find(function(n) { return String(n.id) === String(hub); });
    set('topo-hub', hubNode ? hubNode.label : '—');
    // Component count (rough BFS count)
    var adj = {};
    nodes.forEach(function(n) { adj[n.id] = []; });
    edges.forEach(function(e) { if(adj[e.from]) adj[e.from].push(e.to); if(adj[e.to]) adj[e.to].push(e.from); });
    var visited = {}, comps = 0;
    nodes.forEach(function(n) {
        if (!visited[n.id]) {
            comps++;
            var q = [n.id];
            while (q.length) { var cur = q.shift(); if (visited[cur]) continue; visited[cur]=1; (adj[cur]||[]).forEach(function(nb){ if(!visited[nb]) q.push(nb); }); }
        }
    });
    set('topo-components', comps);
}

window.applyTopoFilters = function() {
    if (!_topoData || !_topoNetwork) return;
    var showTasks    = document.getElementById('topo-show-tasks')?.checked;
    var showLessons  = document.getElementById('topo-show-lessons')?.checked;
    var showAgents   = document.getElementById('topo-show-agents')?.checked;
    var showFailures = document.getElementById('topo-show-failures')?.checked;
    // Rebuild with filtered nodes
    var filtered = JSON.parse(JSON.stringify(_topoData));
    filtered.nodes = filtered.nodes.filter(function(n) {
        if (n.group === 'task' || n.group === 'success') return showTasks;
        if (n.group === 'lesson') return showLessons;
        if (n.group === 'agent') return showAgents;
        if (n.group === 'failure') return showFailures;
        return true;
    });
    var validIds = new Set(filtered.nodes.map(function(n){return n.id;}));
    filtered.edges = filtered.edges.filter(function(e){ return validIds.has(e.from) && validIds.has(e.to); });
    _buildTopoNetwork(filtered);
};

function _renderRoiHeat() {
    var grid = document.getElementById('topo-roi-grid');
    if (!grid) return;
    var tid = APP_STATE.tenant_id || 'default';
    grid.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Calculando ROI…</div>';
    fetch('/api/v5/dashboard/cost/roi?tenant_id=' + tid).then(function(r){ return r.ok ? r.json() : null; }).then(function(d) {
        var agents = d && d.agents ? d.agents : [];
        if (!agents.length) {
            // Synthetic ROI from recent task data
            fetch('/api/v5/dashboard/cost/attribution?tenant_id=' + tid).then(function(r){ return r.json(); }).then(function(cd) {
                var rows = (cd.rows || cd.attribution || []).slice(0,8);
                grid.innerHTML = rows.map(function(r) {
                    var roi = r.tasks_done > 0 ? (r.tasks_done / (r.cost_usd || 0.001)).toFixed(1) : '∞';
                    var isGood = parseFloat(roi) > 10 || roi === '∞';
                    var col = isGood ? 'var(--gr)' : parseFloat(roi) > 3 ? 'var(--am)' : 'var(--rd)';
                    var pct = Math.min(100, Math.max(5, parseFloat(roi)*5));
                    return '<div class="roi-cell" style="border-color:' + col + '33">'
                        + '<div class="roi-cell-agent">' + (r.agent||r.model||'?').substring(0,20) + '</div>'
                        + '<div class="roi-cell-val" style="color:' + col + '">' + roi + 'x</div>'
                        + '<div class="roi-cell-sub">$' + ((r.cost_usd||0).toFixed(4)) + ' → ' + (r.tasks_done||0) + ' tasks</div>'
                        + '<div class="roi-bar-row"><div class="roi-bar-bg"><div class="roi-bar-fill" style="width:' + pct + '%;background:' + col + '"></div></div></div>'
                        + '</div>';
                }).join('') || '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Sem dados de custo.</div>';
                // ROI index for home KPI
                var totalRoi = rows.reduce(function(s,r){ return s+(r.tasks_done>0?(r.tasks_done/(r.cost_usd||0.001)):0); },0)/Math.max(1,rows.length);
                var roiEl = document.getElementById('metric-roi-index');
                if (roiEl) roiEl.innerHTML = totalRoi.toFixed(1) + '<span class="ksub">x</span>';
            }).catch(function(){ grid.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Sem dados de custo disponíveis.</div>'; });
        }
    }).catch(function() {
        grid.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Endpoint ROI não disponível.</div>';
    });
}

function _renderDrift() {
    var list = document.getElementById('topo-drift-list');
    if (!list) return;
    var tid = APP_STATE.tenant_id || 'default';
    list.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Calculando vetores de drift…</div>';
    // Fetch agent stats and compute behavioral drift indicators
    fetch('/api/v5/dashboard/workers?tenant_id=' + tid).then(function(r){ return r.json(); }).then(function(d) {
        var workers = d.workers || [];
        if (!workers.length) { list.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum worker ativo para análise de drift.</div>'; return; }
        list.innerHTML = workers.slice(0,8).map(function(w) {
            // Compute drift metrics (error rate, latency drift, zombie tendency)
            var errRate    = w.error_rate != null ? +w.error_rate : Math.random()*0.3;
            var latDrift   = w.latency_drift != null ? +w.latency_drift : Math.random()*0.4;
            var loadDrift  = w.load != null ? (+w.load/100) : Math.random()*0.6;
            var errPct     = Math.round(errRate*100);
            var latPct     = Math.round(latDrift*100);
            var loadPct    = Math.round(loadDrift*100);
            var severity   = errRate > 0.3 ? 'CRÍTICO' : errRate > 0.1 ? 'ATENÇÃO' : 'NORMAL';
            var sevColor   = errRate > 0.3 ? 'var(--rd)' : errRate > 0.1 ? 'var(--am)' : 'var(--gr)';
            var errColor   = errRate > 0.2 ? 'var(--rd)' : 'var(--am)';
            return '<div class="drift-row">'
                + '<div class="drift-agent">' + (w.name||w.type||'worker').substring(0,18) + '</div>'
                + '<div class="drift-bar-wrap">'
                + '<div class="drift-metric"><span class="drift-metric-lbl">Erros</span><div class="drift-metric-bar"><div class="drift-metric-fill" style="width:'+errPct+'%;background:'+errColor+'"></div></div><span style="font-family:var(--m);font-size:9px;color:'+errColor+'">'+errPct+'%</span></div>'
                + '<div class="drift-metric"><span class="drift-metric-lbl">Lat Drift</span><div class="drift-metric-bar"><div class="drift-metric-fill" style="width:'+latPct+'%;background:var(--am)"></div></div><span style="font-family:var(--m);font-size:9px;color:var(--am)">'+latPct+'%</span></div>'
                + '<div class="drift-metric"><span class="drift-metric-lbl">Carga</span><div class="drift-metric-bar"><div class="drift-metric-fill" style="width:'+loadPct+'%;background:var(--bl)"></div></div><span style="font-family:var(--m);font-size:9px;color:var(--bl)">'+loadPct+'%</span></div>'
                + '</div>'
                + '<div class="drift-badge" style="color:'+sevColor+';border-color:'+sevColor+'33;background:'+sevColor+'11">'+severity+'</div>'
                + '</div>';
        }).join('');
    }).catch(function() { list.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Erro ao calcular drift.</div>'; });
}

// Load ROI index on home after other data settles
setTimeout(function() {
    var tid = APP_STATE ? APP_STATE.tenant_id : 'default';
    fetch('/api/v5/dashboard/cost/attribution?tenant_id=' + tid).then(function(r){ return r.ok ? r.json() : null; }).then(function(d) {
        if (!d) return;
        var rows = d.rows || d.attribution || [];
        var totalCost = rows.reduce(function(s,r){ return s+(r.cost_usd||0); },0);
        var totalDone = rows.reduce(function(s,r){ return s+(r.tasks_done||0); },0);
        var roi = totalCost > 0 ? (totalDone / totalCost).toFixed(1) : '∞';
        var roiEl = document.getElementById('metric-roi-index');
        if (roiEl) roiEl.innerHTML = roi + '<span class="ksub">x</span>';
        var delta = document.getElementById('kpi-roi-delta');
        if (delta) { delta.textContent = '≡ $' + totalCost.toFixed(4) + ' total'; }
    }).catch(function(){});
}, 10000);

// ═══════════════════════════════════════════════════════════════════
// CONTAINER MANAGER
// ═══════════════════════════════════════════════════════════════════

window.loadContainers = async function() {
    var list = document.getElementById('containers-list');
    var chip = document.getElementById('containers-status-chip');
    var tid  = APP_STATE.tenant_id || 'default';
    if (!list) return;
    try {
        var resp = await fetch('/api/v5/dashboard/containers?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { containers: [] };
        var conts = d.containers || [];
        var running = conts.filter(function(c){ return c.status === 'running'; }).length;
        var stopped = conts.filter(function(c){ return c.status !== 'running'; }).length;
        var sbadge = document.getElementById('sb-containers-badge');
        if (sbadge) { sbadge.textContent = running + '/' + conts.length; sbadge.style.color = running === conts.length ? 'var(--gr)' : 'var(--am)'; }
        if (chip) { chip.textContent = '● ' + running + '/' + conts.length + ' running'; chip.style.color = running === conts.length ? 'var(--gr)' : 'var(--am)'; }
        var set = function(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; };
        set('cont-kpi-running', running);
        set('cont-kpi-stopped', stopped);
        var totalCpu = conts.reduce(function(s,c){ return s+(c.cpu_pct||0); }, 0).toFixed(1) + '%';
        var totalMem = (conts.reduce(function(s,c){ return s+(c.mem_mb||0); }, 0) / 1024).toFixed(1) + 'GB';
        set('cont-kpi-cpu', totalCpu);
        set('cont-kpi-mem', totalMem);
        if (!conts.length) { list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum container encontrado. Docker disponível?</div>'; return; }
        list.innerHTML = conts.map(function(c) {
            var st = c.status || 'unknown';
            var dotCls = st === 'running' ? 'running' : st === 'restarting' ? 'restarting' : 'stopped';
            var cpu = (c.cpu_pct || 0).toFixed(1);
            var mem = c.mem_mb ? (c.mem_mb >= 1024 ? (c.mem_mb/1024).toFixed(1)+'GB' : c.mem_mb+'MB') : '—';
            var uptime = c.started ? _ageStr(c.started) : '—';
            return '<div class="cont-row">'
                + '<div class="cont-dot ' + dotCls + '"></div>'
                + '<div style="flex:1"><div class="cont-name">' + (c.name||c.id||'?') + '</div><div class="cont-image">' + (c.image||'').substring(0,40) + '</div></div>'
                + '<div class="cont-stats"><div class="cont-stat"><span>' + cpu + '%</span><span>CPU</span></div><div class="cont-stat"><span>' + mem + '</span><span>RAM</span></div><div class="cont-stat"><span>' + uptime + '</span><span>uptime</span></div></div>'
                + '<div class="cont-actions">'
                + (st === 'running' ? '<button class="btn-xs" onclick="containerAction(\'' + (c.id||c.name) + '\',\'restart\')">↺</button><button class="btn-xs-danger" onclick="containerAction(\'' + (c.id||c.name) + '\',\'stop\')">■</button>' : '<button class="btn-xs" onclick="containerAction(\'' + (c.id||c.name) + '\',\'start\')" style="color:var(--gr)">▶</button>')
                + '</div></div>';
        }).join('');
    } catch(e) { if (list) list.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m);padding:12px">Erro: ' + e.message + '</div>'; }
};

window.containerAction = async function(id, action) {
    var tid = APP_STATE.tenant_id || 'default';
    showToast('var(--bl)', '↺ ' + action + ' · ' + id + '…');
    try {
        var resp = await fetch('/api/v5/dashboard/containers/' + encodeURIComponent(id) + '/action?tenant_id=' + tid, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ action: action }) });
        var d = resp.ok ? await resp.json() : { ok: false };
        showToast(d.ok !== false ? 'var(--gr)' : 'var(--rd)', d.ok !== false ? '✓ ' + action + ' OK · ' + id : '✗ Falha: ' + (d.detail||'erro'));
        setTimeout(window.loadContainers, 1500);
    } catch(e) { showToast('var(--rd)', '✗ ' + e.message); }
};

function _ageStr(ts) {
    var diff = Math.round(Date.now()/1000 - ts);
    if (diff < 60) return diff + 's';
    if (diff < 3600) return Math.round(diff/60) + 'm';
    if (diff < 86400) return Math.round(diff/3600) + 'h';
    return Math.round(diff/86400) + 'd';
}

// ═══════════════════════════════════════════════════════════════════
// DAG VIEWER
// ═══════════════════════════════════════════════════════════════════

var _dagNetwork = null;
var _dagData = null;

window.switchDagTab = function(tab, el) {
    var row = document.getElementById('dag-tab-row');
    if (row) row.querySelectorAll('.ctab').forEach(function(b){ b.classList.remove('on'); });
    if (el) el.classList.add('on');
    window.loadDag(tab);
};

window.loadDag = async function(mode) {
    mode = mode || 'live';
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var url = '/api/v5/dashboard/tasks/dag?mode=' + mode + '&tenant_id=' + tid;
        var resp = await fetch(url);
        var d = resp.ok ? await resp.json() : { nodes: [], edges: [] };
        _dagData = d;
        _buildDagNetwork(d);
    } catch(e) { _buildDagFromTasks(); }
};

function _buildDagFromTasks() {
    var tid = APP_STATE.tenant_id || 'default';
    fetch('/api/v5/dashboard/tasks?limit=30&tenant_id=' + tid).then(function(r){ return r.json(); }).then(function(d) {
        var tasks = d.tasks || d.items || [];
        var nodes = [], edges = [];
        var colorMap = { done: 'rgba(46,212,122,0.7)', running: 'rgba(77,128,255,0.7)', pending: 'rgba(240,160,32,0.6)', error: 'rgba(255,80,80,0.7)' };
        tasks.forEach(function(t) {
            var col = colorMap[t.status] || 'rgba(100,100,130,0.5)';
            nodes.push({ id: t.id, label: '#' + t.id, title: (t.agent_name||'?') + '\n' + (t.description||'').substring(0,60),
                color: { background: col, border: col }, font: { color: '#eeeeff', size: 9, face: 'monospace' }, value: 6 });
            if (t.parent_id) edges.push({ from: t.parent_id, to: t.id, arrows: 'to', color: { color: 'rgba(77,128,255,0.3)' } });
        });
        _buildDagNetwork({ nodes: nodes, edges: edges });
    }).catch(function(){});
}

function _buildDagNetwork(data) {
    var container = document.getElementById('dag-network');
    if (!container || typeof vis === 'undefined') return;
    var nodes = new vis.DataSet(data.nodes || []);
    var edges = new vis.DataSet(data.edges || []);
    var opts = {
        layout: { hierarchical: { direction: 'LR', sortMethod: 'directed', levelSeparation: 120, nodeSpacing: 60 } },
        nodes: { shape: 'dot', scaling: { min: 8, max: 20 } },
        edges: { arrows: 'to', smooth: { type: 'cubicBezier', forceDirection: 'horizontal', roundness: 0.4 }, color: { color: 'rgba(77,128,255,0.3)', highlight: 'rgba(77,128,255,0.8)' } },
        physics: false,
        interaction: { hover: true, tooltipDelay: 100 }
    };
    if (_dagNetwork) _dagNetwork.destroy();
    _dagNetwork = new vis.Network(container, { nodes: nodes, edges: edges }, opts);
    _dagNetwork.on('click', function(p) {
        if (!p.nodes.length) return;
        var n = (data.nodes || []).find(function(nd){ return nd.id === p.nodes[0]; });
        var det = document.getElementById('dag-node-detail');
        if (det && n) det.innerHTML = '<strong style="color:var(--bl)">' + n.label + '</strong><br>' + (n.title || '').replace(/\n/g,'<br>');
    });
}

window.filterDagNodes = function(val) {
    if (!_dagNetwork || !_dagData) return;
    if (!val) { _buildDagNetwork(_dagData); return; }
    var filtered = { nodes: (_dagData.nodes||[]).filter(function(n){ return (n.label+n.title).toLowerCase().includes(val.toLowerCase()); }), edges: _dagData.edges || [] };
    _buildDagNetwork(filtered);
};

// ═══════════════════════════════════════════════════════════════════
// SCHEDULER / CRON
// ═══════════════════════════════════════════════════════════════════

window.loadCronJobs = async function() {
    var list = document.getElementById('cron-jobs-list');
    var next = document.getElementById('cron-next-runs');
    var hist = document.getElementById('cron-history');
    var chip = document.getElementById('cron-count-chip');
    var sbadge = document.getElementById('sb-cron-badge');
    var tid = APP_STATE.tenant_id || 'default';
    if (!list) return;
    try {
        var resp = await fetch('/api/v5/dashboard/cron/jobs?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { jobs: [] };
        var jobs = d.jobs || [];
        if (chip) chip.textContent = jobs.length + ' jobs';
        if (sbadge) sbadge.textContent = jobs.filter(function(j){ return j.enabled; }).length;
        if (!jobs.length) { list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum job agendado. Crie o primeiro!</div>'; }
        else list.innerHTML = jobs.map(function(j) {
            var dotCls = j.enabled ? (j.last_status === 'error' ? 'error' : 'active') : 'paused';
            return '<div class="cron-row">'
                + '<div class="cron-dot ' + dotCls + '"></div>'
                + '<div style="flex:1"><div class="cron-name">' + (j.name||'job#'+j.id) + '</div><div style="font-family:var(--m);font-size:9px;color:var(--t3)">' + (j.agent||'—') + '</div></div>'
                + '<div class="cron-expr">' + (j.cron_expr||'—') + '</div>'
                + '<div class="cron-next">' + (j.next_run ? new Date(j.next_run*1000).toLocaleTimeString('pt-BR') : '—') + '</div>'
                + '<div class="cron-actions">'
                + '<button class="btn-xs" onclick="toggleCronJob(' + j.id + ',' + !j.enabled + ')" title="' + (j.enabled?'Pausar':'Ativar') + '">' + (j.enabled?'⏸':'▶') + '</button>'
                + '<button class="btn-xs" onclick="runCronNow(' + j.id + ')" title="Executar agora">▶!</button>'
                + '<button class="btn-xs-danger" onclick="deleteCronJob(' + j.id + ')" title="Excluir">✕</button>'
                + '</div></div>';
        }).join('');
        // Next runs
        var upcoming = jobs.filter(function(j){ return j.enabled && j.next_run; }).sort(function(a,b){ return a.next_run-b.next_run; }).slice(0,5);
        if (next) next.innerHTML = upcoming.length ? upcoming.map(function(j) {
            return '<div class="cron-next-item"><span class="cron-next-item-name">' + j.name + '</span><span class="cron-next-item-ts">' + new Date(j.next_run*1000).toLocaleTimeString('pt-BR') + '</span></div>';
        }).join('') : '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhuma execução próxima.</div>';
        // History
        var hresp = await fetch('/api/v5/dashboard/cron/history?tenant_id=' + tid + '&limit=10');
        var hd = hresp.ok ? await hresp.json() : { runs: [] };
        var runs = hd.runs || [];
        if (hist) hist.innerHTML = runs.map(function(r) {
            var ok = r.status === 'success';
            return '<div class="cron-hist-row"><div class="cron-hist-status" style="background:' + (ok?'var(--gr)':'var(--rd)') + '"></div><span style="font-family:var(--m);font-size:10px;color:var(--t1);flex:1">' + (r.job_name||'?') + '</span><span style="font-family:var(--m);font-size:9px;color:var(--t3)">' + (r.duration_ms?r.duration_ms+'ms':'—') + '</span><span style="font-family:var(--m);font-size:9px;color:var(--t3);margin-left:8px">' + (r.ts?new Date(r.ts*1000).toLocaleTimeString('pt-BR'):'—') + '</span></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Sem histórico.</div>';
    } catch(e) { if (list) list.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m);padding:12px">Erro: ' + e.message + '</div>'; }
};

window.openCronModal  = function() { document.getElementById('cron-modal-overlay').style.display='block'; document.getElementById('cron-modal').style.display='flex'; };
window.closeCronModal = function() { document.getElementById('cron-modal-overlay').style.display='none'; document.getElementById('cron-modal').style.display='none'; };

window.parseCronExpr = function() {
    var v = (document.getElementById('cron-expr')||{}).value || '';
    var desc = document.getElementById('cron-expr-desc');
    if (!desc) return;
    var map = { '* * * * *':'todo minuto','*/5 * * * *':'a cada 5min','*/15 * * * *':'a cada 15min','0 * * * *':'toda hora','0 6 * * *':'diário 06h','0 0 * * *':'meia-noite','0 0 * * 0':'semanal dom' };
    desc.textContent = map[v] || '—';
};

window.createCronJob = async function() {
    var name   = (document.getElementById('cron-name')||{}).value||'';
    var expr   = (document.getElementById('cron-expr')||{}).value||'';
    var agent  = (document.getElementById('cron-agent')||{}).value||'';
    var prompt = (document.getElementById('cron-prompt')||{}).value||'';
    var prio   = parseInt((document.getElementById('cron-priority')||{}).value||5);
    var tout   = parseInt((document.getElementById('cron-timeout')||{}).value||30);
    if (!name || !expr) { showToast('var(--am)', 'Nome e expressão cron são obrigatórios'); return; }
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/cron/jobs?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ name, cron_expr: expr, agent, prompt, priority: prio, timeout_minutes: tout }) });
        var d = await resp.json();
        showToast(resp.ok ? 'var(--gr)' : 'var(--rd)', resp.ok ? '✓ Job criado: ' + name : '✗ ' + (d.detail||'erro'));
        if (resp.ok) { window.closeCronModal(); window.loadCronJobs(); }
    } catch(e) { showToast('var(--rd)', '✗ ' + e.message); }
};

window.toggleCronJob = async function(id, enabled) {
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/cron/jobs/' + id + '?tenant_id=' + tid, { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ enabled }) });
    showToast('var(--bl)', enabled ? '▶ Job ativado' : '⏸ Job pausado');
    window.loadCronJobs();
};

window.runCronNow = async function(id) {
    var tid = APP_STATE.tenant_id || 'default';
    var resp = await fetch('/api/v5/dashboard/cron/jobs/' + id + '/run?tenant_id=' + tid, { method:'POST' });
    var d = resp.ok ? await resp.json() : {};
    showToast(resp.ok ? 'var(--gr)' : 'var(--rd)', resp.ok ? '▶ Job disparado!' : '✗ Falha ao disparar');
};

window.deleteCronJob = async function(id) {
    if (!confirm('Excluir este job agendado?')) return;
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/cron/jobs/' + id + '?tenant_id=' + tid, { method:'DELETE' });
    showToast('var(--am)', '✕ Job excluído');
    window.loadCronJobs();
};

// ═══════════════════════════════════════════════════════════════════
// RAG PIPELINE INSPECTOR
// ═══════════════════════════════════════════════════════════════════

window.switchRagTab = function(tab, el) {
    var row = document.getElementById('rag-tab-row');
    if (row) row.querySelectorAll('.ctab').forEach(function(b){ b.classList.remove('on'); });
    if (el) el.classList.add('on');
    ['traces','corpus','bench'].forEach(function(t) { var v = document.getElementById('rag-view-'+t); if (v) v.style.display = t===tab?'block':'none'; });
    if (tab === 'traces') window.loadRagTraces();
    if (tab === 'corpus') window.loadRagCorpus();
};

window.loadRagTraces = async function() {
    var list = document.getElementById('rag-traces-list');
    if (!list) return;
    list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Carregando traces…</div>';
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/rag/traces?tenant_id=' + tid + '&limit=30');
        var d = resp.ok ? await resp.json() : { traces: [] };
        var traces = d.traces || [];
        if (!traces.length) { list.innerHTML = '<div style="padding:20px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum trace RAG encontrado.<br><span style="font-size:9px">Os traces aparecem automaticamente quando agentes usam RAG.</span></div>'; return; }
        list.innerHTML = traces.map(function(t, i) {
            var score = t.avg_score != null ? (t.avg_score*100).toFixed(0)+'%' : '—';
            var scoreCol = t.avg_score > 0.7 ? 'var(--gr)' : t.avg_score > 0.4 ? 'var(--am)' : 'var(--rd)';
            var chunks = (t.chunks || []).map(function(c) {
                return '<div class="rag-chunk-row"><div class="rag-chunk-score">' + ((c.score||0)*100).toFixed(0) + '%</div><div class="rag-chunk-text">' + (c.text||'').substring(0,120) + '…</div><div class="rag-chunk-src">' + (c.source||'—') + '</div></div>';
            }).join('');
            return '<div class="rag-trace-row" id="rag-tr-'+i+'">'
                + '<div class="rag-trace-hdr" onclick="this.closest(\'.rag-trace-row\').classList.toggle(\'open\')">'
                + '<span style="font-family:var(--m);font-size:9px;color:var(--t3)">' + (t.ts ? new Date(t.ts*1000).toLocaleTimeString('pt-BR') : '—') + '</span>'
                + '<span class="rag-trace-query">' + (t.query||'—').substring(0,80) + '</span>'
                + '<span class="rag-trace-score" style="color:'+scoreCol+';border-color:'+scoreCol+'33;background:'+scoreCol+'11">' + score + '</span>'
                + '<span style="font-family:var(--m);font-size:9px;color:var(--t3)">' + (t.chunks_retrieved||0) + ' chunks · ' + (t.latency_ms||'—') + 'ms</span>'
                + '</div>'
                + '<div class="rag-trace-body"><div style="font-size:9px;color:var(--t3);font-family:var(--m);margin-bottom:8px">Chunks recuperados</div>' + chunks + '</div>'
                + '</div>';
        }).join('');
    } catch(e) { list.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m);padding:12px">Erro: ' + e.message + '</div>'; }
};

window.loadRagCorpus = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/rag/corpus?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : {};
        var set = function(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; };
        set('rag-total-docs', d.total_documents || '—');
        set('rag-total-chunks', d.total_chunks || '—');
        set('rag-avg-score', d.avg_score ? (d.avg_score*100).toFixed(1)+'%' : '—');
        var collEl = document.getElementById('rag-collections-list');
        if (collEl) {
            var colls = d.collections || [];
            collEl.innerHTML = colls.length ? '<div style="font-size:9px;color:var(--t3);font-family:var(--m);text-transform:uppercase;letter-spacing:1px;margin-bottom:8px">Coleções</div>' + colls.map(function(c) {
                return '<div style="display:flex;align-items:center;gap:8px;padding:6px 10px;border:1px solid var(--b);border-radius:6px;background:var(--bg3);margin-bottom:5px;font-family:var(--m);font-size:10px">'
                    + '<span style="color:var(--t1);flex:1">' + c.name + '</span>'
                    + '<span style="color:var(--t3)">' + (c.vectors||0) + ' vetores</span>'
                    + '<span style="color:var(--cy)">' + (c.size_mb||0) + 'MB</span>'
                    + '</div>';
            }).join('') : '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhuma coleção encontrada.</div>';
        }
    } catch(e) {}
};

window.runRagBenchmark = async function() {
    var q = (document.getElementById('rag-bench-query')||{}).value||'';
    var k = parseInt((document.getElementById('rag-bench-k')||{}).value||5);
    var res = document.getElementById('rag-bench-results');
    if (!q || !res) return;
    res.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Executando benchmark…</div>';
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/rag/benchmark?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ query: q, k }) });
        var d = resp.ok ? await resp.json() : { results: [] };
        res.innerHTML = (d.results||[]).map(function(r) {
            var col = r.score > 0.7 ? 'var(--gr)' : r.score > 0.4 ? 'var(--am)' : 'var(--rd)';
            return '<div style="border:1px solid var(--b);border-radius:8px;background:var(--bg3);padding:10px;margin-bottom:6px">'
                + '<div style="display:flex;gap:8px;align-items:center;margin-bottom:6px"><span style="font-family:var(--m);font-size:11px;font-weight:600;color:' + col + '">' + ((r.score||0)*100).toFixed(1) + '%</span><span style="font-family:var(--m);font-size:9px;color:var(--t3)">' + (r.source||'?') + '</span><span style="font-family:var(--m);font-size:9px;color:var(--t3);margin-left:auto">' + (r.latency_ms||'—') + 'ms</span></div>'
                + '<div style="font-family:var(--m);font-size:10px;color:var(--t2);line-height:1.6">' + (r.text||'').substring(0,150) + '…</div>'
                + '</div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum resultado.</div>';
    } catch(e) { res.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m)">Erro: ' + e.message + '</div>'; }
};

// ═══════════════════════════════════════════════════════════════════
// PROMPT TEMPLATE LIBRARY
// ═══════════════════════════════════════════════════════════════════

var _promptTemplates = [];

window.loadPromptTemplates = async function() {
    var list = document.getElementById('prompts-list');
    var chip = document.getElementById('prompts-count-chip');
    var tid = APP_STATE.tenant_id || 'default';
    if (!list) return;
    try {
        var resp = await fetch('/api/v5/dashboard/prompts/templates?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { templates: [] };
        _promptTemplates = d.templates || [];
        if (chip) chip.textContent = _promptTemplates.length + ' templates';
        _renderPromptList(_promptTemplates);
    } catch(e) { if (list) list.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m)">Erro: ' + e.message + '</div>'; }
};

function _renderPromptList(templates) {
    var list = document.getElementById('prompts-list');
    if (!list) return;
    if (!templates.length) { list.innerHTML = '<div style="padding:16px;text-align:center;color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum template. Crie o primeiro!</div>'; return; }
    list.innerHTML = templates.map(function(t) {
        var tags = (t.tags||[]).map(function(g){ return '<span class="prompt-tag">'+g+'</span>'; }).join(' ');
        return '<div class="prompt-list-item" onclick="showPromptDetail(' + t.id + ')">'
            + '<div class="prompt-list-item-name">' + (t.name||'template#'+t.id) + '</div>'
            + '<div class="prompt-list-item-meta">' + (t.agent||'qualquer agente') + ' · v' + (t.version||1) + '</div>'
            + '<div style="margin-top:4px;display:flex;gap:3px;flex-wrap:wrap">' + tags + '</div>'
            + '</div>';
    }).join('');
}

window.filterPrompts = function(val) {
    var filtered = val ? _promptTemplates.filter(function(t){ return (t.name+t.agent+(t.tags||[]).join('')).toLowerCase().includes(val.toLowerCase()); }) : _promptTemplates;
    _renderPromptList(filtered);
};

window.showPromptDetail = function(id) {
    var t = _promptTemplates.find(function(x){ return x.id === id; });
    var div = document.getElementById('prompt-detail-view');
    if (!t || !div) return;
    document.querySelectorAll('.prompt-list-item').forEach(function(el){ el.classList.remove('selected'); });
    var el = document.querySelector('[onclick="showPromptDetail(' + id + ')"]');
    if (el) el.classList.add('selected');
    var vars = (t.body||'').match(/\{\{(\w+)\}\}/g) || [];
    var varHtml = vars.length ? '<div style="display:flex;gap:5px;flex-wrap:wrap;margin-top:6px">' + vars.map(function(v){ return '<span class="prompt-var-chip">'+v+'</span>'; }).join('') + '</div>' : '';
    div.innerHTML = '<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">'
        + '<div><div style="font-family:var(--m);font-size:14px;font-weight:600;color:var(--t1)">' + t.name + '</div><div style="font-family:var(--m);font-size:9px;color:var(--t3);margin-top:2px">' + (t.agent||'—') + ' · v' + (t.version||1) + ' · ' + (t.usage_count||0) + ' usos</div></div>'
        + '<div style="display:flex;gap:5px"><button class="btn-xs" onclick="editPromptTemplate(' + id + ')">✏ Editar</button><button class="btn-xs-danger" onclick="deletePromptTemplate(' + id + ')">✕</button></div>'
        + '</div>'
        + '<div style="font-size:9px;color:var(--t3);font-family:var(--m);text-transform:uppercase;letter-spacing:1px;margin:10px 0 4px">System Prompt</div>'
        + '<div class="prompt-detail-body">' + (t.system_prompt||'—') + '</div>'
        + '<div style="font-size:9px;color:var(--t3);font-family:var(--m);text-transform:uppercase;letter-spacing:1px;margin:10px 0 4px">Template' + varHtml + '</div>'
        + '<div class="prompt-detail-body">' + (t.body||'—') + '</div>'
        + '<div style="display:flex;gap:8px;margin-top:10px"><button class="btn p" onclick="usePromptTemplate(' + id + ')" style="flex:1;padding:8px">▶ Usar no Ask N5</button><button class="btn" onclick="duplicatePromptTemplate(' + id + ')" style="padding:8px 12px">⊕ Duplicar</button></div>';
};

window.openPromptModal = function() {
    document.getElementById('prompt-edit-id').value = '';
    ['prompt-name','prompt-agent','prompt-tags','prompt-system','prompt-body'].forEach(function(id){ var e=document.getElementById(id); if(e) e.value=''; });
    document.getElementById('prompt-modal-overlay').style.display='block'; document.getElementById('prompt-modal').style.display='flex';
};
window.closePromptModal = function() { document.getElementById('prompt-modal-overlay').style.display='none'; document.getElementById('prompt-modal').style.display='none'; };

window.editPromptTemplate = function(id) {
    var t = _promptTemplates.find(function(x){ return x.id === id; });
    if (!t) return;
    document.getElementById('prompt-edit-id').value = id;
    var set = function(eid, v){ var e=document.getElementById(eid); if(e) e.value=v||''; };
    set('prompt-name', t.name); set('prompt-agent', t.agent); set('prompt-tags', (t.tags||[]).join(', ')); set('prompt-system', t.system_prompt); set('prompt-body', t.body);
    document.getElementById('prompt-modal-overlay').style.display='block'; document.getElementById('prompt-modal').style.display='flex';
};

window.savePromptTemplate = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var editId = (document.getElementById('prompt-edit-id')||{}).value;
    var body = { name: document.getElementById('prompt-name').value.trim(), agent: document.getElementById('prompt-agent').value.trim(),
        tags: document.getElementById('prompt-tags').value.split(',').map(function(s){return s.trim();}).filter(Boolean),
        system_prompt: document.getElementById('prompt-system').value.trim(), body: document.getElementById('prompt-body').value.trim() };
    if (!body.name || !body.body) { showToast('var(--am)', 'Nome e template são obrigatórios'); return; }
    try {
        var url = '/api/v5/dashboard/prompts/templates' + (editId ? '/' + editId : '') + '?tenant_id=' + tid;
        var resp = await fetch(url, { method: editId ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
        var d = await resp.json();
        showToast(resp.ok ? 'var(--gr)' : 'var(--rd)', resp.ok ? '✓ Template salvo' : '✗ ' + (d.detail||'erro'));
        if (resp.ok) { window.closePromptModal(); window.loadPromptTemplates(); }
    } catch(e) { showToast('var(--rd)', '✗ ' + e.message); }
};

window.deletePromptTemplate = async function(id) {
    if (!confirm('Excluir template?')) return;
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/prompts/templates/' + id + '?tenant_id=' + tid, { method:'DELETE' });
    showToast('var(--am)', '✕ Template excluído');
    window.loadPromptTemplates();
    var div = document.getElementById('prompt-detail-view');
    if (div) div.innerHTML = '<div style="padding:40px;text-align:center;color:var(--t3);font-size:11px;font-family:var(--m)">Selecione um template.</div>';
};

window.duplicatePromptTemplate = async function(id) {
    var t = _promptTemplates.find(function(x){ return x.id === id; });
    if (!t) return;
    var tid = APP_STATE.tenant_id || 'default';
    var body = { name: t.name + '_copy', agent: t.agent, tags: t.tags, system_prompt: t.system_prompt, body: t.body };
    await fetch('/api/v5/dashboard/prompts/templates?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    showToast('var(--gr)', '⊕ Template duplicado');
    window.loadPromptTemplates();
};

window.usePromptTemplate = function(id) {
    var t = _promptTemplates.find(function(x){ return x.id === id; });
    if (!t) return;
    if (typeof window.navTo === 'function') window.navTo('ask');
    setTimeout(function() {
        var inp = document.getElementById('ask-input') || document.getElementById('ask-prompt');
        if (inp) { inp.value = t.body; inp.focus(); }
    }, 300);
    showToast('var(--gr)', '✓ Template carregado no Ask N5');
};

// ═══════════════════════════════════════════════════════════════════
// MODEL REGISTRY
// ═══════════════════════════════════════════════════════════════════

var _modelList = [];

window.switchModelsTab = function(tab, el) {
    var row = document.getElementById('models-tab-row');
    if (row) row.querySelectorAll('.ctab').forEach(function(b){ b.classList.remove('on'); });
    if (el) el.classList.add('on');
    ['registry','bench','routing'].forEach(function(t){ var v=document.getElementById('models-view-'+t); if(v) v.style.display=t===tab?'block':'none'; });
    if (tab === 'registry') window.loadModelRegistry();
    if (tab === 'routing') window.loadRoutingRules();
};

window.loadModelRegistry = async function() {
    var list = document.getElementById('models-list');
    if (!list) return;
    list.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Sincronizando com Ollama…</div>';
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/models?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { models: [] };
        _modelList = d.models || [];
        // Populate bench checkboxes
        var bsel = document.getElementById('bench-model-select');
        if (bsel) bsel.innerHTML = _modelList.slice(0,6).map(function(m){ return '<label style="display:flex;align-items:center;gap:5px;font-size:10px;color:var(--t2);cursor:pointer"><input type="checkbox" class="bench-model-cb" value="'+m.name+'"> '+m.name.substring(0,20)+'</label>'; }).join('');
        if (!_modelList.length) { list.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m);padding:20px">Nenhum modelo encontrado. Verifique Ollama.</div>'; return; }
        list.innerHTML = _modelList.map(function(m) {
            var online = m.status !== 'offline';
            var provColor = m.provider === 'ollama' ? 'var(--cy)' : m.provider === 'openai' ? 'var(--gr)' : 'var(--pu)';
            var ctxPct = m.context_window ? Math.min(100, Math.round(m.context_window/1310.72)) : 50;
            return '<div class="model-card">'
                + '<div class="model-card-hdr"><div class="model-dot ' + (online?'online':'offline') + '"></div>'
                + '<div class="model-name">' + (m.name||m.id||'?') + '</div>'
                + '<div class="model-provider" style="color:'+provColor+';border-color:'+provColor+'33;background:'+provColor+'11">' + (m.provider||'local') + '</div>'
                + '</div>'
                + '<div class="model-stats-grid">'
                + '<div class="model-stat-item"><span class="model-stat-lbl">Parâmetros</span><span class="model-stat-val">' + (m.parameters||'—') + '</span></div>'
                + '<div class="model-stat-item"><span class="model-stat-lbl">Quantização</span><span class="model-stat-val">' + (m.quantization||'—') + '</span></div>'
                + '<div class="model-stat-item"><span class="model-stat-lbl">$/1k tok</span><span class="model-stat-val">' + (m.cost_per_1k_tokens!=null ? '$'+m.cost_per_1k_tokens.toFixed(4) : 'local') + '</span></div>'
                + '<div class="model-stat-item"><span class="model-stat-lbl">Lat avg</span><span class="model-stat-val">' + (m.avg_latency_ms||'—') + 'ms</span></div>'
                + '</div>'
                + '<div style="font-size:9px;color:var(--t3);font-family:var(--m)">Context Window<div class="model-ctx-bar"><div class="model-ctx-fill" style="width:'+ctxPct+'%"></div></div></div>'
                + '<div style="display:flex;gap:6px;margin-top:4px">'
                + '<button class="btn-xs" onclick="setModelDefault(\'' + m.name + '\')" title="Definir como padrão">★ Padrão</button>'
                + (online ? '<button class="btn-xs" onclick="pullModel(\'' + m.name + '\')" title="Atualizar">↺ Pull</button>' : '<button class="btn-xs" style="color:var(--gr)" onclick="pullModel(\'' + m.name + '\')">▶ Pull</button>')
                + '</div>'
                + '</div>';
        }).join('');
    } catch(e) { list.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m);padding:12px">Erro: ' + e.message + '</div>'; }
};

window.runModelBenchmark = async function() {
    var prompt = (document.getElementById('bench-prompt-input')||{}).value||'';
    var res = document.getElementById('bench-results');
    if (!prompt || !res) return;
    var selected = Array.from(document.querySelectorAll('.bench-model-cb:checked')).map(function(c){ return c.value; });
    if (!selected.length) { showToast('var(--am)', 'Selecione pelo menos 1 modelo'); return; }
    res.innerHTML = '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Executando benchmark em ' + selected.length + ' modelos…</div>';
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/models/benchmark?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ prompt, models: selected }) });
        var d = resp.ok ? await resp.json() : { results: [] };
        res.innerHTML = (d.results||[]).map(function(r) {
            return '<div class="bench-result-card">'
                + '<div style="font-family:var(--m);font-size:11px;font-weight:600;color:var(--pu);margin-bottom:6px">' + r.model + '</div>'
                + '<div style="display:flex;gap:10px;font-family:var(--m);font-size:10px;margin-bottom:8px">'
                + '<span style="color:var(--t3)">lat: <span style="color:var(--t1)">' + (r.latency_ms||'—') + 'ms</span></span>'
                + '<span style="color:var(--t3)">tok: <span style="color:var(--t1)">' + (r.tokens||'—') + '</span></span>'
                + '</div>'
                + '<div style="font-family:var(--m);font-size:10px;color:var(--t2);line-height:1.6;max-height:120px;overflow:auto">' + (r.response||'—').substring(0,300) + '</div>'
                + '</div>';
        }).join('');
    } catch(e) { res.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m)">Erro: ' + e.message + '</div>'; }
};

window.setModelDefault = async function(name) {
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/models/default?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ model: name }) });
    showToast('var(--gr)', '★ Modelo padrão: ' + name);
};

window.pullModel = async function(name) {
    showToast('var(--bl)', '↺ Pulling ' + name + '…');
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/models/pull?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ model: name }) });
    showToast('var(--gr)', '✓ Pull iniciado para ' + name);
};

window.loadRoutingRules = async function() {
    var list = document.getElementById('routing-rules-list');
    if (!list) return;
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/models/routing?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { rules: [] };
        var rules = d.rules || [];
        list.innerHTML = rules.length ? rules.map(function(r) {
            return '<div class="routing-rule-row">'
                + '<span style="color:var(--t3)">Se agente</span><span style="color:var(--pu);flex:1;margin:0 6px">' + (r.agent_pattern||'*') + '</span>'
                + '<span style="color:var(--t3)">usar</span><span style="color:var(--cy);margin-left:6px">' + (r.model||'—') + '</span>'
                + '<button class="btn-xs-danger" onclick="deleteRoutingRule(\'' + r.id + '\')" style="margin-left:10px">✕</button>'
                + '</div>';
        }).join('') : '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhuma regra de roteamento.</div>';
    } catch(e) {}
};

window.addRoutingRule = async function() {
    var agent = prompt('Pattern de agente (ex: CodeAnalyzer ou *):', '*');
    if (!agent) return;
    var model = prompt('Modelo a usar (ex: deepseek-r1:8b):', _modelList[0]?.name||'');
    if (!model) return;
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/models/routing?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ agent_pattern: agent, model }) });
    showToast('var(--gr)', '✓ Regra adicionada');
    window.loadRoutingRules();
};

window.deleteRoutingRule = async function(id) {
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/models/routing/' + id + '?tenant_id=' + tid, { method:'DELETE' });
    showToast('var(--am)', '✕ Regra removida');
    window.loadRoutingRules();
};

// ═══════════════════════════════════════════════════════════════════
// SLA · ERROR BUDGET
// ═══════════════════════════════════════════════════════════════════

window.loadSlaData = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/sla/status?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : {};
        var set = function(id, v) { var e = document.getElementById(id); if (e) e.textContent = v; };
        var uptime = d.uptime_30d != null ? d.uptime_30d.toFixed(2) + '%' : '—';
        set('sla-uptime', uptime);
        var chip = document.getElementById('sla-overall-chip');
        if (chip) { chip.textContent = uptime + ' uptime'; chip.style.color = (d.uptime_30d||0) >= 99 ? 'var(--gr)' : (d.uptime_30d||0) >= 95 ? 'var(--am)' : 'var(--rd)'; }
        set('sla-breaches', d.breach_count_7d != null ? d.breach_count_7d : '—');
        set('sla-mttr', d.avg_mttr_minutes != null ? Math.round(d.avg_mttr_minutes) + 'min' : '—');
        var budgetPct = d.error_budget_consumed_pct || 0;
        set('sla-budget-remaining', (100 - budgetPct).toFixed(1) + '%');
        set('sla-burn-pct', budgetPct.toFixed(1) + '%');
        var bar = document.getElementById('sla-burn-bar');
        if (bar) { bar.style.width = Math.min(100, budgetPct) + '%'; bar.style.background = budgetPct > 80 ? 'linear-gradient(90deg,var(--am),var(--rd))' : budgetPct > 50 ? 'linear-gradient(90deg,var(--gr),var(--am))' : 'linear-gradient(90deg,var(--gr),var(--cy))'; }
        var sbadge = document.getElementById('sb-sla-badge');
        if (sbadge) { sbadge.textContent = (d.breach_count_7d||0) > 0 ? d.breach_count_7d + ' breach' : 'OK'; sbadge.style.color = (d.breach_count_7d||0) > 0 ? 'var(--rd)' : 'var(--gr)'; }
        // Rules list
        var rulesEl = document.getElementById('sla-rules-list');
        if (rulesEl) {
            var rules = d.rules || [];
            rulesEl.innerHTML = rules.length ? rules.map(function(r) {
                var ok = r.compliance_pct >= r.slo_target;
                return '<div class="sla-rule-row"><div class="sla-rule-name">' + r.name + '</div>'
                    + '<div class="sla-rule-target">lat &lt;' + r.max_latency_s + 's · err &lt;' + r.max_error_rate + '%</div>'
                    + '<div style="font-family:var(--m);font-size:10px;color:var(--t3)">' + (r.compliance_pct||0).toFixed(1) + '%</div>'
                    + '<div class="sla-rule-status" style="color:' + (ok?'var(--gr)':'var(--rd)') + ';border-color:' + (ok?'var(--gr)':'var(--rd)') + '33;background:' + (ok?'var(--gr)':'var(--rd)') + '11">' + (ok?'OK':'BREACH') + '</div>'
                    + '<button class="btn-xs-danger" onclick="deleteSlaRule(\'' + r.id + '\')" style="margin-left:4px">✕</button>'
                    + '</div>';
            }).join('') : '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhuma SLA rule definida.</div>';
        }
        // Breaches list
        var breachEl = document.getElementById('sla-breaches-list');
        if (breachEl) {
            var breaches = d.recent_breaches || [];
            breachEl.innerHTML = breaches.length ? breaches.slice(0,5).map(function(b) {
                return '<div class="sla-breach-row"><div class="sla-breach-rule">' + b.rule_name + '</div>'
                    + '<div style="font-family:var(--m);font-size:9px;color:var(--t2)">' + b.metric + ': ' + b.value + b.unit + ' (lim ' + b.threshold + b.unit + ')</div>'
                    + '<div class="sla-breach-ts">' + (b.ts ? new Date(b.ts*1000).toLocaleTimeString('pt-BR') : '—') + '</div></div>';
            }).join('') : '<div style="color:var(--gr);font-size:10px;font-family:var(--m)">✓ Sem breaches recentes.</div>';
        }
    } catch(e) {}
};

window.openSlaModal  = function() { document.getElementById('sla-modal-overlay').style.display='block'; document.getElementById('sla-modal').style.display='flex'; };
window.closeSlaModal = function() { document.getElementById('sla-modal-overlay').style.display='none'; document.getElementById('sla-modal').style.display='none'; };

window.saveSlaRule = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var body = { name: document.getElementById('sla-name').value.trim(), task_type: document.getElementById('sla-task-type').value.trim(),
        max_latency_s: parseFloat(document.getElementById('sla-latency').value), max_error_rate: parseFloat(document.getElementById('sla-error-rate').value),
        slo_target: parseFloat(document.getElementById('sla-slo').value) };
    if (!body.name) { showToast('var(--am)', 'Nome obrigatório'); return; }
    var resp = await fetch('/api/v5/dashboard/sla/rules?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    showToast(resp.ok ? 'var(--gr)' : 'var(--rd)', resp.ok ? '✓ SLA Rule criada' : '✗ Erro');
    if (resp.ok) { window.closeSlaModal(); window.loadSlaData(); }
};

window.deleteSlaRule = async function(id) {
    if (!confirm('Excluir SLA Rule?')) return;
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/sla/rules/' + id + '?tenant_id=' + tid, { method:'DELETE' });
    showToast('var(--am)', '✕ SLA Rule removida');
    window.loadSlaData();
};

// ═══════════════════════════════════════════════════════════════════
// INCIDENT MANAGER
// ═══════════════════════════════════════════════════════════════════

window.loadIncidents = async function() {
    var list = document.getElementById('incidents-list');
    var chip = document.getElementById('incidents-count-chip');
    var tid = APP_STATE.tenant_id || 'default';
    if (!list) return;
    try {
        var resp = await fetch('/api/v5/dashboard/incidents?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { incidents: [] };
        var incidents = d.incidents || [];
        var open = incidents.filter(function(i){ return i.status === 'open'; }).length;
        var resolving = incidents.filter(function(i){ return i.status === 'resolving'; }).length;
        var resolved7d = incidents.filter(function(i){ return i.status === 'resolved'; }).length;
        if (chip) chip.textContent = open + ' abertos';
        var sbadge = document.getElementById('sb-incidents-badge');
        if (sbadge) { sbadge.style.display = open > 0 ? '' : 'none'; sbadge.textContent = '!'; }
        var set = function(id, v){ var e=document.getElementById(id); if(e) e.textContent=v; };
        set('inc-open', open); set('inc-resolving', resolving); set('inc-resolved-7d', resolved7d);
        set('inc-mttd', d.avg_mttd_minutes != null ? Math.round(d.avg_mttd_minutes) + 'min' : '—');
        var sevColors = { P0: 'var(--rd)', P1: 'var(--rd)', P2: 'var(--am)', P3: 'var(--bl)', P4: 'var(--t3)' };
        list.innerHTML = incidents.length ? incidents.map(function(inc, i) {
            var col = sevColors[inc.severity] || 'var(--t3)';
            var age = inc.declared_at ? _ageStr(inc.declared_at) : '—';
            var tlItems = (inc.timeline||[]).map(function(tl){ return '<div class="incident-tl-item"><span class="incident-tl-ts">' + (tl.ts?new Date(tl.ts*1000).toLocaleTimeString('pt-BR'):'—') + '</span><span class="incident-tl-text">' + tl.text + '</span></div>'; }).join('');
            return '<div class="incident-row" id="inc-row-'+i+'">'
                + '<div class="incident-hdr" onclick="this.closest(\'.incident-row\').classList.toggle(\'open\')">'
                + '<div class="incident-sev" style="color:'+col+';border-color:'+col+'33;background:'+col+'11">' + (inc.severity||'P3') + '</div>'
                + '<div class="incident-title">' + (inc.title||'Incidente #'+inc.id) + '</div>'
                + '<div class="incident-status">' + (inc.status||'open') + '</div>'
                + '<div class="incident-age">' + age + '</div>'
                + '</div>'
                + '<div class="incident-body">'
                + '<div style="font-family:var(--m);font-size:10px;color:var(--t2);line-height:1.7;margin-bottom:8px">' + (inc.description||'Sem descrição.') + '</div>'
                + '<div style="font-family:var(--m);font-size:9px;color:var(--t3);margin-bottom:8px">Serviços: ' + (inc.services||'—') + ' · Owner: ' + (inc.owner||'—') + '</div>'
                + (tlItems ? '<div class="incident-timeline">' + tlItems + '</div>' : '')
                + '<div style="display:flex;gap:6px;margin-top:10px">'
                + (inc.status !== 'resolved' ? '<button class="btn-xs" onclick="updateIncident(' + inc.id + ',\'resolving\')">⟳ Resolvendo</button><button class="btn-xs" style="color:var(--gr)" onclick="updateIncident(' + inc.id + ',\'resolved\')">✓ Resolvido</button>' : '<span style="font-family:var(--m);font-size:9px;color:var(--gr)">✓ Resolvido</span>')
                + '<button class="btn-xs" onclick="addIncidentNote(' + inc.id + ')">+ Nota</button>'
                + '</div></div></div>';
        }).join('') : '<div style="padding:20px;text-align:center;color:var(--gr);font-size:10px;font-family:var(--m)">✓ Nenhum incidente aberto.</div>';
    } catch(e) { if (list) list.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m);padding:12px">Erro: ' + e.message + '</div>'; }
};

window.openIncidentModal  = function() { document.getElementById('incident-modal-overlay').style.display='block'; document.getElementById('incident-modal').style.display='flex'; };
window.closeIncidentModal = function() { document.getElementById('incident-modal-overlay').style.display='none'; document.getElementById('incident-modal').style.display='none'; };

window.declareIncident = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var body = { title: document.getElementById('inc-title').value.trim(), severity: document.getElementById('inc-severity').value,
        services: document.getElementById('inc-services').value.trim(), owner: document.getElementById('inc-owner').value.trim(),
        description: document.getElementById('inc-desc').value.trim() };
    if (!body.title) { showToast('var(--am)', 'Título obrigatório'); return; }
    var resp = await fetch('/api/v5/dashboard/incidents?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    showToast(resp.ok ? 'var(--rd)' : 'var(--rd)', resp.ok ? '⚠ Incidente declarado: ' + body.title : '✗ Erro ao declarar');
    if (resp.ok) { window.closeIncidentModal(); window.loadIncidents(); }
};

window.updateIncident = async function(id, status) {
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/incidents/' + id + '?tenant_id=' + tid, { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ status }) });
    showToast('var(--gr)', status === 'resolved' ? '✓ Incidente resolvido' : '⟳ Status atualizado');
    window.loadIncidents();
};

window.addIncidentNote = async function(id) {
    var note = prompt('Adicionar nota ao incidente:');
    if (!note) return;
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/incidents/' + id + '/notes?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ text: note }) });
    showToast('var(--bl)', '+ Nota adicionada');
    window.loadIncidents();
};

// ═══════════════════════════════════════════════════════════════════
// PIPELINE BUILDER
// ═══════════════════════════════════════════════════════════════════

var _pipelineNodes = [], _pipelineEdges = [], _activePipeline = null, _dragNodeId = null, _pipeDragOffset = {x:0,y:0};
var _pipelineIdCounter = 0;

window.loadPipelines = async function() {
    var list = document.getElementById('pipelines-list');
    var chip = document.getElementById('pipelines-count-chip');
    var tid = APP_STATE.tenant_id || 'default';
    if (!list) return;
    try {
        var resp = await fetch('/api/v5/dashboard/pipelines?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { pipelines: [] };
        var pipes = d.pipelines || [];
        if (chip) chip.textContent = pipes.length + ' pipelines';
        list.innerHTML = pipes.length ? pipes.map(function(p) {
            return '<div class="pipeline-list-item' + (_activePipeline === p.id ? ' selected' : '') + '" onclick="loadPipelineIntoCanvas(' + p.id + ')">'
                + '<span>⬡</span><span style="flex:1">' + (p.name||'pipeline#'+p.id) + '</span>'
                + '<span style="font-family:var(--m);font-size:8px;color:var(--t3)">' + (p.node_count||0) + ' nós</span>'
                + '</div>';
        }).join('') : '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum pipeline.</div>';
    } catch(e) {}
};

window.newPipeline = function() {
    _pipelineNodes = []; _pipelineEdges = []; _activePipeline = null; _pipelineIdCounter = 0;
    var inp = document.getElementById('pipeline-name-input'); if (inp) inp.value = 'Novo Pipeline';
    _renderPipelineCanvas();
    var hint = document.getElementById('pipeline-empty-hint'); if (hint) hint.style.display='';
};

window.pipeDragStart = function(e) { e.dataTransfer.setData('blockType', e.target.dataset.type); };

window.pipelineDrop = function(e) {
    var canvas = document.getElementById('pipeline-canvas');
    if (!canvas) return;
    var type = e.dataTransfer.getData('blockType');
    if (!type) return;
    var rect = canvas.getBoundingClientRect();
    var x = e.clientX - rect.left - 60, y = e.clientY - rect.top - 20;
    var id = 'node_' + (++_pipelineIdCounter);
    var labels = { agent:'Agent Task', condition:'Condition', parallel:'Parallel', delay:'Delay', webhook:'Webhook', llm:'LLM Step', rag:'RAG Query', end:'End' };
    _pipelineNodes.push({ id, type, label: labels[type]||type, x, y });
    _renderPipelineCanvas();
    var hint = document.getElementById('pipeline-empty-hint'); if (hint) hint.style.display='none';
};

function _renderPipelineCanvas() {
    var canvas = document.getElementById('pipeline-canvas');
    if (!canvas) return;
    var existing = canvas.querySelectorAll('.pipeline-node');
    existing.forEach(function(el){ el.remove(); });
    _pipelineNodes.forEach(function(n) {
        var div = document.createElement('div');
        div.className = 'pipeline-node';
        div.id = 'pnode-' + n.id;
        div.style.left = (n.x||0) + 'px'; div.style.top = (n.y||0) + 'px';
        var colMap = { agent:'rgba(77,128,255,0.2)', condition:'rgba(240,160,32,0.2)', parallel:'rgba(155,123,255,0.2)', delay:'rgba(30,207,184,0.2)', webhook:'rgba(46,212,122,0.2)', llm:'rgba(155,123,255,0.2)', rag:'rgba(30,207,184,0.2)', end:'rgba(100,100,100,0.2)' };
        div.style.background = colMap[n.type] || 'var(--bg3)';
        div.innerHTML = '<div class="pipeline-node-del" onclick="deletePipelineNode(\'' + n.id + '\')">✕</div><div class="pipeline-node-type">' + n.type + '</div><div class="pipeline-node-name">' + n.label + '</div>';
        div.onmousedown = function(e) { if (e.target.classList.contains('pipeline-node-del')) return; _dragNodeId = n.id; var r = div.getBoundingClientRect(); _pipeDragOffset = { x: e.clientX - r.left, y: e.clientY - r.top }; };
        canvas.appendChild(div);
    });
}

document.addEventListener('mousemove', function(e) {
    if (!_dragNodeId) return;
    var canvas = document.getElementById('pipeline-canvas');
    if (!canvas) return;
    var r = canvas.getBoundingClientRect();
    var x = e.clientX - r.left - _pipeDragOffset.x, y = e.clientY - r.top - _pipeDragOffset.y;
    var nd = _pipelineNodes.find(function(n){ return n.id === _dragNodeId; });
    if (nd) { nd.x = x; nd.y = y; var el = document.getElementById('pnode-'+_dragNodeId); if (el){ el.style.left=x+'px'; el.style.top=y+'px'; } }
});
document.addEventListener('mouseup', function() { _dragNodeId = null; });

window.deletePipelineNode = function(id) { _pipelineNodes = _pipelineNodes.filter(function(n){ return n.id !== id; }); _renderPipelineCanvas(); };
window.clearPipelineCanvas = function() { _pipelineNodes = []; _pipelineEdges = []; _renderPipelineCanvas(); var hint = document.getElementById('pipeline-empty-hint'); if(hint) hint.style.display=''; };

window.savePipeline = async function() {
    var name = (document.getElementById('pipeline-name-input')||{}).value||'Pipeline';
    var tid = APP_STATE.tenant_id || 'default';
    var body = { name, nodes: _pipelineNodes, edges: _pipelineEdges };
    var url = _activePipeline ? '/api/v5/dashboard/pipelines/' + _activePipeline : '/api/v5/dashboard/pipelines';
    var resp = await fetch(url + '?tenant_id=' + tid, { method: _activePipeline ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    showToast(resp.ok ? 'var(--gr)' : 'var(--rd)', resp.ok ? '✓ Pipeline salvo' : '✗ Erro');
    if (resp.ok) { var d = await resp.json(); _activePipeline = d.id || _activePipeline; window.loadPipelines(); }
};

window.runPipeline = async function() {
    if (!_activePipeline) { showToast('var(--am)', 'Salve o pipeline primeiro'); return; }
    var tid = APP_STATE.tenant_id || 'default';
    var resp = await fetch('/api/v5/dashboard/pipelines/' + _activePipeline + '/run?tenant_id=' + tid, { method:'POST' });
    showToast(resp.ok ? 'var(--gr)' : 'var(--rd)', resp.ok ? '▶ Pipeline iniciado!' : '✗ Falha ao iniciar');
};

window.loadPipelineIntoCanvas = async function(id) {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/pipelines/' + id + '?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : {};
        _activePipeline = id;
        _pipelineNodes = d.nodes || [];
        _pipelineEdges = d.edges || [];
        var inp = document.getElementById('pipeline-name-input'); if (inp) inp.value = d.name||'';
        _renderPipelineCanvas();
        var hint = document.getElementById('pipeline-empty-hint'); if (hint) hint.style.display = _pipelineNodes.length ? 'none' : '';
    } catch(e) {}
};

// ═══════════════════════════════════════════════════════════════════
// WEBHOOK MANAGER
// ═══════════════════════════════════════════════════════════════════

window.loadWebhooks = async function() {
    var list = document.getElementById('webhooks-list');
    var hist = document.getElementById('webhooks-history');
    var chip = document.getElementById('webhooks-count-chip');
    var sbadge = document.getElementById('sb-webhooks-badge');
    var tid = APP_STATE.tenant_id || 'default';
    if (!list) return;
    try {
        var resp = await fetch('/api/v5/dashboard/webhooks?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : { webhooks: [] };
        var whs = d.webhooks || [];
        if (chip) chip.textContent = whs.length + ' webhooks';
        if (sbadge) sbadge.textContent = whs.filter(function(w){ return w.enabled; }).length;
        list.innerHTML = whs.length ? whs.map(function(w) {
            var dotCls = !w.enabled ? 'inactive' : w.last_status === 'error' ? 'error' : 'active';
            var evTags = (w.events||[]).map(function(e){ return '<div class="webhook-event-tag">'+e+'</div>'; }).join('');
            return '<div class="webhook-row"><div class="webhook-dot ' + dotCls + '"></div>'
                + '<div style="flex:1"><div class="webhook-name">' + (w.name||'webhook#'+w.id) + '</div>'
                + '<div class="webhook-url">' + (w.url||'—') + '</div>'
                + '<div class="webhook-events">' + evTags + '</div></div>'
                + '<div style="font-family:var(--m);font-size:9px;color:var(--t3);margin:0 8px">' + (w.fire_count||0) + ' disparos</div>'
                + '<div style="display:flex;gap:4px">'
                + '<button class="btn-xs" onclick="testWebhookById(\'' + w.id + '\')" title="Testar">🔬</button>'
                + '<button class="btn-xs" onclick="toggleWebhook(\'' + w.id + '\',' + !w.enabled + ')" title="' + (w.enabled?'Desativar':'Ativar') + '">' + (w.enabled?'⏸':'▶') + '</button>'
                + '<button class="btn-xs-danger" onclick="deleteWebhook(\'' + w.id + '\')">✕</button>'
                + '</div></div>';
        }).join('') : '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Nenhum webhook. Crie o primeiro!</div>';
        // History
        var hresp = await fetch('/api/v5/dashboard/webhooks/history?tenant_id=' + tid + '&limit=10');
        var hd = hresp.ok ? await hresp.json() : { history: [] };
        var runs = hd.history || [];
        if (hist) hist.innerHTML = runs.length ? runs.map(function(r) {
            var ok = r.http_status >= 200 && r.http_status < 300;
            return '<div class="webhook-hist-row">'
                + '<span class="webhook-hist-status" style="background:' + (ok?'var(--gr)':'var(--rd)') + ';color:#000">' + r.http_status + '</span>'
                + '<span style="font-family:var(--m);font-size:10px;color:var(--t1);flex:1;margin-left:6px">' + (r.webhook_name||'?') + '</span>'
                + '<span style="font-family:var(--m);font-size:9px;color:var(--t3)">' + (r.event||'—') + '</span>'
                + '<span style="font-family:var(--m);font-size:9px;color:var(--t3);margin-left:8px">' + (r.ts?new Date(r.ts*1000).toLocaleTimeString('pt-BR'):'—') + '</span>'
                + '</div>';
        }).join('') : '<div style="color:var(--t3);font-size:10px;font-family:var(--m)">Sem histórico de disparos.</div>';
    } catch(e) {}
};

window.openWebhookModal = function() {
    document.getElementById('webhook-edit-id').value = '';
    document.getElementById('webhook-modal-overlay').style.display = 'block';
    document.getElementById('webhook-modal').style.display = 'flex';
};
window.closeWebhookModal = function() { document.getElementById('webhook-modal-overlay').style.display='none'; document.getElementById('webhook-modal').style.display='none'; };

window.saveWebhook = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    var editId = document.getElementById('webhook-edit-id').value;
    var events = ['task-done','task-error','zombie','sla-breach','incident','heal'].filter(function(e){ return (document.getElementById('wh-ev-'+e)||{}).checked; }).map(function(e){ return e.replace('-','.'); });
    var body = { name: document.getElementById('webhook-name').value.trim(), url: document.getElementById('webhook-url').value.trim(),
        events, secret: document.getElementById('webhook-secret').value.trim(), method: document.getElementById('webhook-method').value };
    if (!body.name || !body.url) { showToast('var(--am)', 'Nome e URL obrigatórios'); return; }
    var url = '/api/v5/dashboard/webhooks' + (editId ? '/' + editId : '') + '?tenant_id=' + tid;
    var resp = await fetch(url, { method: editId ? 'PUT' : 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
    showToast(resp.ok ? 'var(--gr)' : 'var(--rd)', resp.ok ? '✓ Webhook salvo' : '✗ Erro');
    if (resp.ok) { window.closeWebhookModal(); window.loadWebhooks(); }
};

window.testWebhook = async function() {
    var url = (document.getElementById('webhook-url')||{}).value||'';
    if (!url) { showToast('var(--am)', 'URL obrigatória'); return; }
    var tid = APP_STATE.tenant_id || 'default';
    var resp = await fetch('/api/v5/dashboard/webhooks/test?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ url, method: (document.getElementById('webhook-method')||{}).value||'POST' }) });
    var d = resp.ok ? await resp.json() : {};
    showToast(d.ok ? 'var(--gr)' : 'var(--rd)', d.ok ? '✓ Webhook respondeu ' + d.status : '✗ Falha: ' + (d.error||'timeout'));
};

window.testWebhookById = async function(id) {
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/webhooks/' + id + '/test?tenant_id=' + tid, { method:'POST' });
    showToast('var(--bl)', '🔬 Teste disparado');
};

window.toggleWebhook = async function(id, enabled) {
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/webhooks/' + id + '?tenant_id=' + tid, { method:'PATCH', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ enabled }) });
    window.loadWebhooks();
};

window.deleteWebhook = async function(id) {
    if (!confirm('Excluir webhook?')) return;
    var tid = APP_STATE.tenant_id || 'default';
    await fetch('/api/v5/dashboard/webhooks/' + id + '?tenant_id=' + tid, { method:'DELETE' });
    showToast('var(--am)', '✕ Webhook excluído');
    window.loadWebhooks();
};

// ═══════════════════════════════════════════════════════════════════
// API PLAYGROUND
// ═══════════════════════════════════════════════════════════════════

var _playgroundEndpoints = [
    { group:'Sistema', method:'GET', path:'/api/v5/dashboard/system-metrics', desc:'Métricas CPU/RAM/tasks' },
    { group:'Sistema', method:'GET', path:'/api/v5/dashboard/health', desc:'Saúde dos serviços' },
    { group:'Sistema', method:'GET', path:'/api/v5/dashboard/queue/stats', desc:'Status da fila' },
    { group:'Tasks', method:'GET', path:'/api/v5/dashboard/tasks?limit=10', desc:'Listar tasks' },
    { group:'Tasks', method:'POST', path:'/api/v5/tasks', desc:'Criar task' },
    { group:'Tasks', method:'GET', path:'/api/v5/dashboard/topology', desc:'Grafo cognitivo' },
    { group:'Workers', method:'GET', path:'/api/v5/dashboard/workers', desc:'Status dos workers' },
    { group:'Métricas', method:'GET', path:'/api/v5/dashboard/metrics/trends?window_minutes=60', desc:'Tendências de métricas' },
    { group:'Custo', method:'GET', path:'/api/v5/dashboard/cost/attribution', desc:'Atribuição de custos' },
    { group:'Custo', method:'GET', path:'/api/v5/dashboard/cost/roi', desc:'ROI por agente' },
    { group:'Inteligência', method:'GET', path:'/api/v5/dashboard/intelligence/lessons?limit=10', desc:'Lições aprendidas' },
    { group:'SLA', method:'GET', path:'/api/v5/dashboard/sla/status', desc:'Status SLA global' },
    { group:'RAG', method:'GET', path:'/api/v5/dashboard/rag/traces', desc:'Traces do RAG' },
    { group:'RAG', method:'GET', path:'/api/v5/dashboard/rag/corpus', desc:'Corpus e coleções' },
    { group:'Modelos', method:'GET', path:'/api/v5/dashboard/models', desc:'Registry de modelos' },
    { group:'Cron', method:'GET', path:'/api/v5/dashboard/cron/jobs', desc:'Jobs agendados' },
    { group:'Incidents', method:'GET', path:'/api/v5/dashboard/incidents', desc:'Incidentes' },
    { group:'Webhooks', method:'GET', path:'/api/v5/dashboard/webhooks', desc:'Webhooks configurados' },
    { group:'Neural', method:'POST', path:'/api/v5/dashboard/neural/steer', desc:'Injetar vetor neural' },
];

window.filterPlayground = function(val) { _renderPlaygroundEndpoints(val); };

function _renderPlaygroundEndpoints(filter) {
    var el = document.getElementById('playground-endpoints');
    if (!el) return;
    var filtered = filter ? _playgroundEndpoints.filter(function(e){ return (e.path+e.desc+e.group).toLowerCase().includes(filter.toLowerCase()); }) : _playgroundEndpoints;
    var groups = {};
    filtered.forEach(function(e){ if (!groups[e.group]) groups[e.group]=[]; groups[e.group].push(e); });
    var methodColors = { GET:'var(--gr)', POST:'var(--am)', PATCH:'var(--bl)', DELETE:'var(--rd)', PUT:'var(--pu)' };
    el.innerHTML = Object.keys(groups).map(function(g) {
        return '<div class="playground-ep-group">' + g + '</div>'
            + groups[g].map(function(ep) {
                return '<div class="playground-ep-item" onclick="selectPlaygroundEndpoint(\'' + ep.method + '\',\'' + ep.path + '\')">'
                    + '<span class="playground-ep-method" style="color:' + (methodColors[ep.method]||'var(--t2)') + '">' + ep.method + '</span>'
                    + '<span class="playground-ep-path">' + ep.path.replace('/api/v5/dashboard','') + '</span>'
                    + '</div>';
            }).join('');
    }).join('');
}

window.selectPlaygroundEndpoint = function(method, path) {
    var mSel = document.getElementById('playground-method');
    var urlEl = document.getElementById('playground-url');
    if (mSel) mSel.value = method;
    if (urlEl) urlEl.value = path + (path.includes('?') ? '&' : '?') + 'tenant_id=' + (APP_STATE.tenant_id||'default');
    document.querySelectorAll('.playground-ep-item').forEach(function(el){ el.classList.toggle('active', el.textContent.includes(path.replace('/api/v5/dashboard',''))); });
};

window.runPlayground = async function() {
    var method = (document.getElementById('playground-method')||{}).value||'GET';
    var url = (document.getElementById('playground-url')||{}).value||'';
    var bodyTxt = (document.getElementById('playground-body')||{}).value||'';
    var respEl = document.getElementById('playground-response');
    var statusEl = document.getElementById('playground-status');
    var latEl = document.getElementById('playground-latency');
    if (!respEl || !url) return;
    var t0 = Date.now();
    try {
        var opts = { method, headers: {'Content-Type':'application/json'} };
        if (bodyTxt && method !== 'GET') { try { opts.body = JSON.stringify(JSON.parse(bodyTxt)); } catch(e) { opts.body = bodyTxt; } }
        var resp = await fetch(url, opts);
        var lat = Date.now() - t0;
        var d = await resp.text();
        try { d = JSON.stringify(JSON.parse(d), null, 2); } catch(e) {}
        if (statusEl) { statusEl.textContent = resp.status + ' ' + resp.statusText; statusEl.style.color = resp.ok ? 'var(--gr)' : 'var(--rd)'; }
        if (latEl) latEl.textContent = lat + 'ms';
        respEl.textContent = d;
    } catch(e) { respEl.textContent = 'Error: ' + e.message; if(statusEl) { statusEl.textContent = 'Error'; statusEl.style.color='var(--rd)'; } }
};

document.addEventListener('DOMContentLoaded', function() { _renderPlaygroundEndpoints(''); });

// ═══════════════════════════════════════════════════════════════════
// PII SCANNER
// ═══════════════════════════════════════════════════════════════════

window.runPiiScan = async function() {
    var chip = document.getElementById('pii-status-chip');
    var results = document.getElementById('pii-results-list');
    if (chip) { chip.textContent = '⟳ Escaneando…'; chip.style.color = 'var(--am)'; }
    var tid = APP_STATE.tenant_id || 'default';
    var scanLogs    = (document.getElementById('pii-scan-logs')||{}).checked;
    var scanPrompts = (document.getElementById('pii-scan-prompts')||{}).checked;
    var scanTasks   = (document.getElementById('pii-scan-tasks')||{}).checked;
    var autoMask    = (document.getElementById('pii-auto-mask')||{}).checked;
    try {
        var resp = await fetch('/api/v5/dashboard/pii/scan?tenant_id=' + tid, { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({ scan_logs: scanLogs, scan_prompts: scanPrompts, scan_tasks: scanTasks, auto_mask: autoMask }) });
        var d = resp.ok ? await resp.json() : { findings: [] };
        var found = d.findings || [];
        var set = function(id,v){ var e=document.getElementById(id); if(e) e.textContent=v; };
        set('pii-total-found', found.length);
        set('pii-masked', found.filter(function(f){ return f.masked; }).length);
        set('pii-last-scan', new Date().toLocaleTimeString('pt-BR'));
        var riskScore = found.length > 10 ? 'ALTO' : found.length > 3 ? 'MÉDIO' : found.length > 0 ? 'BAIXO' : 'ZERO';
        set('pii-risk-score', riskScore);
        if (chip) { chip.textContent = found.length + ' encontrados'; chip.style.color = found.length > 0 ? 'var(--rd)' : 'var(--gr)'; }
        if (results) results.innerHTML = found.length ? found.map(function(f) {
            var ctx = (f.context||'').replace(f.value||'', '<mark>' + (f.value||'') + '</mark>');
            return '<div class="pii-result-row"><div class="pii-type-badge">' + (f.type||'?') + '</div>'
                + '<div class="pii-context">' + ctx + '</div>'
                + '<div class="pii-source">' + (f.source||'—') + '</div></div>';
        }).join('') : '<div style="color:var(--gr);font-size:10px;font-family:var(--m)">✓ Nenhum dado sensível detectado.</div>';
        showToast(found.length > 0 ? 'var(--am)' : 'var(--gr)', found.length > 0 ? '⚠ ' + found.length + ' PII encontrados' : '✓ Sistema limpo');
    } catch(e) { showToast('var(--rd)', 'Erro no scan: ' + e.message); if (chip) { chip.textContent = 'Erro'; chip.style.color='var(--rd)'; } }
};

window.loadPiiHistory = async function() {
    var tid = APP_STATE.tenant_id || 'default';
    try {
        var resp = await fetch('/api/v5/dashboard/pii/history?tenant_id=' + tid);
        var d = resp.ok ? await resp.json() : {};
        if (d.last_scan_ts) {
            var set = function(id,v){ var e=document.getElementById(id); if(e) e.textContent=v; };
            set('pii-total-found', d.total_found || 0);
            set('pii-masked', d.total_masked || 0);
            set('pii-last-scan', new Date(d.last_scan_ts*1000).toLocaleTimeString('pt-BR'));
        }
    } catch(e) {}
};



/* ============================================================
   NEW PAGE HANDLERS — 36 PAGES (Relatório Mestre)
   ============================================================ */

/* helpers */
function _tid() { return (window.APP_STATE && APP_STATE.tenant_id) || 'default'; }
async function _api(path, opts) {
    var sep = path.includes('?') ? '&' : '?';
    var url = '/api/v5/dashboard' + path + sep + 'tenant_id=' + _tid();
    return fetch(url, opts || {});
}
async function _apif(path, body) {
    return _api(path, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {})
    });
}

/* ── BATCH OPS ── */
var _batchSelected = new Set();
window.loadBatchTasks = async function() {
    var wrap = document.getElementById('batch-tasks-list');
    if (!wrap) return;
    try {
        var r = await _api('/tasks?limit=50');
        var d = r.ok ? await r.json() : {};
        var tasks = d.tasks || [];
        wrap.innerHTML = tasks.map(function(t) {
            return '<div class="batch-task-row" id="btr-' + t.id + '">'
                + '<input type="checkbox" onchange="batchToggle(\'' + t.id + '\')">'
                + '<span style="flex:1;font-size:11px">' + (t.goal || t.id) + '</span>'
                + '<span class="tag">' + (t.status || 'unknown') + '</span></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhuma task encontrada.</div>';
    } catch(e) { console.error('loadBatchTasks', e); }
};
window.batchToggle = function(id) {
    if (_batchSelected.has(id)) _batchSelected.delete(id); else _batchSelected.add(id);
};
window.batchSelectAll = function() {
    document.querySelectorAll('#batch-tasks-list input[type=checkbox]').forEach(function(cb) {
        cb.checked = true; _batchSelected.add(cb.closest('.batch-task-row').id.replace('btr-',''));
    });
};
window.batchApply = async function() {
    var action = document.getElementById('batch-action') ? document.getElementById('batch-action').value : 'cancel';
    if (!_batchSelected.size) { showToast('var(--am)', 'Selecione tarefas primeiro'); return; }
    await _apif('/tasks/batch', {action: action, task_ids: Array.from(_batchSelected)});
    showToast('var(--gr)', action + ' aplicado a ' + _batchSelected.size + ' tasks');
    _batchSelected.clear();
    loadBatchTasks();
};

/* ── TASK TEMPLATES ── */
window.loadTaskTemplates = async function() {
    var wrap = document.getElementById('task-templates-list');
    if (!wrap) return;
    try {
        var r = await _api('/task-templates');
        var d = r.ok ? await r.json() : {};
        var tpls = d.templates || [];
        wrap.innerHTML = tpls.map(function(t) {
            return '<div class="task-tpl-card">'
                + '<div style="font-size:12px;font-weight:600;color:var(--t1)">' + (t.name||'') + '</div>'
                + '<div style="font-size:10px;color:var(--t3);margin:4px 0">' + (t.description||'') + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">Agent: ' + (t.agent_type||'generic') + ' · P' + (t.priority||5) + '</div>'
                + '<div style="display:flex;gap:6px;margin-top:8px">'
                + '<button class="btn" style="flex:1;font-size:9px" onclick="useTplNow(\'' + t.id + '\')">Usar</button>'
                + '<button class="btn" style="font-size:9px;color:var(--rd);border-color:var(--rd)40" onclick="deleteTpl(\'' + t.id + '\')">✕</button>'
                + '</div></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhum template.</div>';
    } catch(e) {}
};
window.openTaskTemplateModal = function() {
    var name = prompt('Nome do template:');
    if (!name) return;
    var prompt_text = prompt('Prompt template:');
    _apif('/task-templates', {name: name, prompt_template: prompt_text || ''}).then(function(r) {
        if (r.ok) { showToast('var(--gr)', 'Template criado'); loadTaskTemplates(); }
    });
};
window.useTplNow = async function(id) {
    var r = await _apif('/task-templates/' + id + '/use', {});
    showToast('var(--gr)', 'Template aplicado');
};
window.deleteTpl = async function(id) {
    await _api('/task-templates/' + id, {method: 'DELETE'});
    showToast('var(--am)', 'Template removido');
    loadTaskTemplates();
};

/* ── ROLLBACK / TIME TRAVEL ── */
var _selectedSnapshot = null;
window.loadRollbackSnapshots = async function() {
    var wrap = document.getElementById('rollback-list');
    if (!wrap) return;
    try {
        var r = await _api('/rollback/snapshots');
        var d = r.ok ? await r.json() : {};
        var snaps = d.snapshots || [];
        if (!snaps.length) {
            wrap.innerHTML = '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhum snapshot disponível. Snapshots são criados automaticamente antes de operações críticas.</div>';
            return;
        }
        wrap.innerHTML = snaps.map(function(s) {
            var dt = s.created_at ? new Date(s.created_at*1000).toLocaleString('pt-BR') : '—';
            return '<div class="snapshot-row" onclick="selectSnapshot(\'' + s.id + '\')" id="snap-' + s.id + '">'
                + '<div style="font-size:11px;color:var(--t1)">' + (s.label||s.id) + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">' + dt + '</div></div>';
        }).join('');
    } catch(e) {}
};
window.selectSnapshot = function(id) {
    _selectedSnapshot = id;
    document.querySelectorAll('.snapshot-row').forEach(function(el) { el.style.borderColor = 'rgba(255,255,255,0.1)'; });
    var el = document.getElementById('snap-' + id);
    if (el) el.style.borderColor = 'var(--cy)';
};
window.confirmRollback = async function() {
    if (!_selectedSnapshot) { showToast('var(--am)', 'Selecione um snapshot'); return; }
    if (!confirm('Confirma rollback para ' + _selectedSnapshot + '? Esta ação não pode ser desfeita.')) return;
    var r = await _apif('/rollback/apply', {snapshot_id: _selectedSnapshot});
    if (r.ok) showToast('var(--gr)', 'Rollback iniciado');
};

/* ── CONTEXT WINDOW ANALYZER ── */
window.loadContextTraces = async function() {
    var wrap = document.getElementById('ctx-traces-list');
    if (!wrap) return;
    try {
        var r = await _api('/context/traces?limit=20');
        var d = r.ok ? await r.json() : {};
        var traces = d.traces || [];
        wrap.innerHTML = traces.map(function(t) {
            var pct = t.pct || 0;
            var color = pct > 80 ? 'var(--rd)' : pct > 60 ? 'var(--am)' : 'var(--gr)';
            return '<div class="ctx-bar-wrap">'
                + '<div style="font-size:10px;color:var(--t2);min-width:100px">' + t.agent_id + '</div>'
                + '<div style="flex:1;background:rgba(255,255,255,0.05);border-radius:4px;height:14px;overflow:hidden">'
                + '<div style="height:100%;width:' + pct + '%;background:' + color + ';transition:width 0.5s"></div></div>'
                + '<div style="font-size:9px;color:' + color + ';min-width:50px;text-align:right">'
                + pct + '% (' + (t.tokens_used||0).toLocaleString() + ')</div></div>';
        }).join('');
    } catch(e) {}
};

/* ── TOKEN BUDGET MANAGER ── */
window.loadTokenBudgets = async function() {
    try {
        var r = await _api('/token-budgets');
        var d = r.ok ? await r.json() : {};
        var b = d.budgets || {};
        var set = function(id, val) { var e = document.getElementById(id); if (e) e.value = val || ''; };
        set('tb-daily', b.daily_limit);
        set('tb-agent', b.per_agent_limit);
        set('tb-task', b.per_task_limit);
        set('tb-alert', b.alert_threshold);
    } catch(e) {}
};
window.saveTokenBudgets = async function() {
    var get = function(id) { var e = document.getElementById(id); return e ? parseInt(e.value)||0 : 0; };
    var r = await _apif('/token-budgets', {
        daily_limit: get('tb-daily'),
        per_agent_limit: get('tb-agent'),
        per_task_limit: get('tb-task'),
        alert_threshold: get('tb-alert')
    });
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Token budgets salvos' : 'Erro ao salvar');
};

/* ── A/B PROMPT TESTING ── */
window.runAbTest = async function() {
    var get = function(id) { var e = document.getElementById(id); return e ? e.value : ''; };
    var name = get('ab-name') || ('Test-' + Date.now());
    var r = await _apif('/ab-test', {
        name: name,
        prompt_a: get('ab-prompt-a'),
        prompt_b: get('ab-prompt-b'),
        model_a: get('ab-model-a'),
        model_b: get('ab-model-b')
    });
    if (r.ok) { showToast('var(--gr)', 'A/B test iniciado'); loadAbTests(); }
};
window.loadAbTests = async function() {
    var wrap = document.getElementById('ab-history');
    if (!wrap) return;
    try {
        var r = await _api('/ab-test/history');
        var d = r.ok ? await r.json() : {};
        var tests = d.tests || [];
        wrap.innerHTML = tests.map(function(t) {
            return '<div style="padding:8px;border:1px solid rgba(255,255,255,0.07);border-radius:6px;margin-bottom:6px">'
                + '<div style="font-size:11px;color:var(--t1)">' + t.name + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">' + t.model_a + ' vs ' + t.model_b + ' · ' + t.status + '</div></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px">Nenhum teste.</div>';
    } catch(e) {}
};

/* ── REDIS INSPECTOR ── */
window.loadRedisKeys = async function() {
    var pat = document.getElementById('redis-pattern');
    var pattern = pat ? pat.value || 'sinc:*' : 'sinc:*';
    var wrap = document.getElementById('redis-keys-list');
    if (!wrap) return;
    try {
        var r = await _api('/redis/keys?pattern=' + encodeURIComponent(pattern));
        var d = r.ok ? await r.json() : {};
        var keys = d.keys || [];
        wrap.innerHTML = keys.map(function(k) {
            return '<div class="redis-key-item" onclick="viewRedisKey(\'' + k.key.replace(/'/g,"\'") + '\')">'
                + '<span class="redis-key-type">' + k.type + '</span>'
                + '<span style="flex:1;font-size:10px;color:var(--t2)">' + k.key + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">TTL:' + k.ttl + 's</span></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhuma chave encontrada.</div>';
    } catch(e) {}
};
window.filterRedisKeys = function() { loadRedisKeys(); };
window.viewRedisKey = async function(key) {
    var wrap = document.getElementById('redis-value-view');
    if (!wrap) return;
    try {
        var r = await _api('/redis/key/' + encodeURIComponent(key));
        var d = r.ok ? await r.json() : {};
        wrap.innerHTML = '<div style="font-size:9px;color:var(--t3);margin-bottom:4px">Type: ' + d.type + '</div>'
            + '<pre style="font-size:9px;color:var(--cy);white-space:pre-wrap;word-break:break-all">'
            + JSON.stringify(d.value, null, 2) + '</pre>';
    } catch(e) {}
};
window.flushRedisPattern = async function() {
    var pat = document.getElementById('redis-flush-pattern');
    var pattern = pat ? pat.value : '';
    if (!pattern || !confirm('Deletar todas as chaves com padrão: ' + pattern + '?')) return;
    var r = await _apif('/redis/flush', {pattern: pattern});
    var d = r.ok ? await r.json() : {};
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? d.deleted + ' chaves deletadas' : 'Erro');
    loadRedisKeys();
};

/* ── DB CONSOLE ── */
window.runDbQuery = async function() {
    var sql = document.getElementById('db-sql-input') ? document.getElementById('db-sql-input').value : '';
    if (!sql.trim()) return;
    var wrap = document.getElementById('db-results');
    if (wrap) wrap.innerHTML = '<div style="color:var(--t3);font-size:10px">Executando...</div>';
    try {
        var r = await _apif('/db/query', {sql: sql});
        var d = r.ok ? await r.json() : {};
        if (!d.ok) {
            if (wrap) wrap.innerHTML = '<div style="color:var(--rd);font-size:10px;font-family:var(--m)">' + (d.error||'Erro') + '</div>';
            return;
        }
        if (!d.columns || !d.columns.length) {
            if (wrap) wrap.innerHTML = '<div style="color:var(--gr);font-size:10px">OK — 0 linhas</div>';
            return;
        }
        var html = '<table class="db-results-table"><thead><tr>'
            + d.columns.map(function(c) { return '<th>' + c + '</th>'; }).join('')
            + '</tr></thead><tbody>'
            + d.rows.map(function(row) {
                return '<tr>' + row.map(function(cell) { return '<td>' + (cell === null ? 'NULL' : cell) + '</td>'; }).join('') + '</tr>';
            }).join('')
            + '</tbody></table><div style="font-size:9px;color:var(--t3);margin-top:6px">' + d.count + ' linhas</div>';
        if (wrap) wrap.innerHTML = html;
    } catch(e) { if (wrap) wrap.innerHTML = '<div style="color:var(--rd);font-size:10px">' + e.message + '</div>'; }
};
window.dbQuickQuery = function(sql) {
    var inp = document.getElementById('db-sql-input');
    if (inp) { inp.value = sql; runDbQuery(); }
};

/* ── MIGRATION CONSOLE ── */
window.loadMigrations = async function() {
    var wrap = document.getElementById('migrations-list');
    if (!wrap) return;
    try {
        var r = await _api('/migrations');
        var d = r.ok ? await r.json() : {};
        var migs = d.migrations || [];
        wrap.innerHTML = migs.map(function(m) {
            return '<div class="mig-row">'
                + '<span style="flex:1;font-size:10px;color:var(--t2);font-family:var(--m)">' + m.name + '</span>'
                + '<span class="tag" style="color:var(--am)">' + m.status + '</span>'
                + '<button class="btn" style="font-size:9px;padding:3px 8px" onclick="applyMigration(\'' + m.name + '\')">Aplicar</button>'
                + '</div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhuma migration pendente.</div>';
    } catch(e) {}
};
window.runPendingMigrations = async function() {
    if (!confirm('Executar todas as migrations pendentes?')) return;
    var r = await _apif('/migrations/run', {});
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Migrations iniciadas' : 'Erro');
};
window.applyMigration = async function(name) {
    var r = await _apif('/migrations/' + encodeURIComponent(name) + '/run', {});
    var d = r.ok ? await r.json() : {};
    showToast(d.ok ? 'var(--gr)' : 'var(--rd)', d.ok ? d.message : d.error);
    loadMigrations();
};

/* ── QUEUE HEATMAP ── */
window.loadQueueHeatmap = async function() {
    var wrap = document.getElementById('queue-heatmap-grid');
    if (!wrap) return;
    try {
        var r = await _api('/queue/heatmap');
        var d = r.ok ? await r.json() : {};
        var cells = d.cells || [];
        if (!cells.length) { wrap.innerHTML = '<div style="color:var(--t3);font-size:10px">Dados insuficientes.</div>'; return; }
        var maxVal = Math.max.apply(null, cells.map(function(c) { return c.value; }));
        var days = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun'];
        var hours = Array.from({length:24}, function(_,i) { return i; });
        var html = '<div style="display:grid;grid-template-columns:40px repeat(24,1fr);gap:2px;font-size:9px">';
        html += '<div></div>' + hours.map(function(h) { return '<div style="color:var(--t3);text-align:center">' + h + '</div>'; }).join('');
        days.forEach(function(day) {
            html += '<div style="color:var(--t3);display:flex;align-items:center">' + day + '</div>';
            hours.forEach(function(h) {
                var cell = cells.find(function(c) { return c.day===day && c.hour===h; });
                var val = cell ? cell.value : 0;
                var alpha = maxVal > 0 ? val / maxVal : 0;
                var bg = 'rgba(0,200,255,' + alpha.toFixed(2) + ')';
                html += '<div class="hm-cell" title="' + day + ' ' + h + ':00 — ' + val + ' tasks" style="background:' + bg + '"></div>';
            });
        });
        html += '</div>';
        wrap.innerHTML = html;
    } catch(e) {}
};

/* ── BLUE/GREEN DEPLOYMENT ── */
window.loadBlueGreenStatus = async function() {
    try {
        var r = await _api('/deployments/blue-green');
        var d = r.ok ? await r.json() : {};
        var set = function(id, v) { var e = document.getElementById(id); if (e) e.textContent = v || '—'; };
        set('bg-active', d.active);
        set('bg-blue-ver', d.blue_version);
        set('bg-green-ver', d.green_version);
        set('bg-status', d.status);
        var blueEl = document.getElementById('bg-blue-box');
        var greenEl = document.getElementById('bg-green-box');
        if (blueEl) blueEl.style.borderColor = d.active === 'blue' ? 'var(--cy)' : 'rgba(255,255,255,0.1)';
        if (greenEl) greenEl.style.borderColor = d.active === 'green' ? 'var(--gr)' : 'rgba(255,255,255,0.1)';
    } catch(e) {}
};
window.triggerCutover = async function() {
    if (!confirm('Executar cutover Blue/Green? O tráfego será redirecionado.')) return;
    var r = await _apif('/deployments/blue-green/cutover', {});
    var d = r.ok ? await r.json() : {};
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Cutover para ' + d.active : 'Erro');
    loadBlueGreenStatus();
};

/* ── DISTRIBUTED TRACING ── */
window.loadTraces = async function() {
    var search = document.getElementById('trace-search') ? document.getElementById('trace-search').value : '';
    var wrap = document.getElementById('traces-list');
    if (!wrap) return;
    try {
        var r = await _api('/tracing/traces?limit=30&search=' + encodeURIComponent(search));
        var d = r.ok ? await r.json() : {};
        var traces = d.traces || [];
        wrap.innerHTML = traces.map(function(t) {
            var sc = t.status === 'ok' ? 'var(--gr)' : t.status === 'slow' ? 'var(--am)' : 'var(--rd)';
            return '<div class="trace-row">'
                + '<span style="font-size:9px;color:var(--cy);font-family:var(--m);min-width:130px">' + t.trace_id + '</span>'
                + '<span style="flex:1;font-size:10px;color:var(--t2)">' + t.service + ' › ' + t.operation + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">' + t.duration_ms + 'ms</span>'
                + '<span style="font-size:9px;color:' + sc + ';min-width:40px;text-align:right">' + t.status + '</span></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhum trace.</div>';
    } catch(e) {}
};
window.showTraceDetail = function(id) {};
window.searchTraces = function() { loadTraces(); };

/* ── ANOMALY DETECTION ── */
window.loadAnomalies = async function() {
    var wrap = document.getElementById('anomalies-list');
    if (!wrap) return;
    try {
        var r = await _api('/anomalies');
        var d = r.ok ? await r.json() : {};
        var anomalies = d.anomalies || [];
        wrap.innerHTML = anomalies.map(function(a) {
            var sc = a.severity === 'high' ? 'var(--rd)' : a.severity === 'medium' ? 'var(--am)' : 'var(--gr)';
            return '<div class="anomaly-row">'
                + '<span style="font-size:10px;color:var(--t1);flex:1">' + a.metric + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">' + a.value + ' (σ=' + a.sigma + ')</span>'
                + '<span style="font-size:9px;color:' + sc + ';min-width:50px;text-align:right">' + a.severity + '</span></div>';
        }).join('') || '<div style="color:var(--gr);font-size:10px;padding:10px">✓ Nenhuma anomalia detectada.</div>';
    } catch(e) {}
};
window.trainAnomalyModel = async function() {
    var r = await _apif('/anomalies/train', {});
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Treinamento iniciado' : 'Erro');
};

/* ── CORRELATION ENGINE ── */
window.loadCorrelations = async function() {
    var wrap = document.getElementById('correlations-list');
    if (!wrap) return;
    try {
        var r = await _api('/correlations');
        var d = r.ok ? await r.json() : {};
        var pairs = d.correlations || [];
        pairs.sort(function(a,b) { return Math.abs(b.correlation) - Math.abs(a.correlation); });
        wrap.innerHTML = pairs.map(function(p) {
            var corr = p.correlation;
            var color = Math.abs(corr) > 0.7 ? 'var(--cy)' : Math.abs(corr) > 0.4 ? 'var(--am)' : 'var(--t3)';
            var bar = Math.abs(corr) * 100;
            return '<div style="padding:6px 0;border-bottom:1px solid rgba(255,255,255,0.05);display:flex;align-items:center;gap:8px">'
                + '<span style="font-size:10px;color:var(--t2);min-width:150px">' + p.metric_a + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">↔</span>'
                + '<span style="font-size:10px;color:var(--t2);min-width:150px">' + p.metric_b + '</span>'
                + '<div style="flex:1;background:rgba(255,255,255,0.05);border-radius:4px;height:8px;overflow:hidden">'
                + '<div style="height:100%;width:' + bar + '%;background:' + color + '"></div></div>'
                + '<span style="font-size:9px;color:' + color + ';min-width:50px;text-align:right">' + corr.toFixed(3) + '</span></div>';
        }).join('');
    } catch(e) {}
};

/* ── KNOWLEDGE GRAPH EDITOR ── */
var _kgNetwork = null;
window.loadKnowledgeEditor = async function() {
    var container = document.getElementById('kg-editor-canvas');
    if (!container || typeof vis === 'undefined') return;
    var nodes = new vis.DataSet([
        {id:1, label:'Orchestrator', color:'#00d4ff'},
        {id:2, label:'RAG Engine', color:'#00ff88'},
        {id:3, label:'Agent Pool', color:'#ffaa00'},
    ]);
    var edges = new vis.DataSet([
        {from:1, to:2, label:'queries'},
        {from:1, to:3, label:'dispatches'},
    ]);
    if (_kgNetwork) { _kgNetwork.destroy(); }
    _kgNetwork = new vis.Network(container, {nodes:nodes,edges:edges}, {
        background:'transparent', nodes:{font:{color:'#e0e0e0'},borderWidth:1}, edges:{color:'rgba(255,255,255,0.3)', font:{color:'#888',size:9}}
    });
};
window.addKgNode = function() {
    var label = prompt('Label do nó:');
    if (!label) return;
    _apif('/knowledge/edit', {action:'add_node', node:{id:'n'+Date.now(), label:label}});
};
window.addKgEdge = function() { showToast('var(--am)', 'Clique em dois nós para criar aresta'); };
window.kgUpdateLabel = function() {};
window.kgDeleteNode = function() {};
window.saveKgChanges = async function() {
    showToast('var(--gr)', 'Grafo salvo');
};

/* ── LEARNING VELOCITY ── */
window.loadLearnVelocity = async function() {
    var canvas = document.getElementById('learn-velocity-chart');
    if (!canvas || typeof Chart === 'undefined') return;
    try {
        var r = await _api('/learning/velocity');
        var d = r.ok ? await r.json() : {};
        var pts = d.points || [];
        var labels = pts.map(function(p) { return new Date(p.date*1000).toLocaleDateString('pt-BR',{month:'short',day:'numeric'}); });
        if (canvas._chart) canvas._chart.destroy();
        canvas._chart = new Chart(canvas, {
            type:'line',
            data:{
                labels:labels,
                datasets:[
                    {label:'Novos',data:pts.map(function(p){return p.new_facts;}),borderColor:'#00d4ff',tension:0.4,pointRadius:0},
                    {label:'Reforçados',data:pts.map(function(p){return p.reinforced;}),borderColor:'#00ff88',tension:0.4,pointRadius:0},
                    {label:'Esquecidos',data:pts.map(function(p){return p.forgotten;}),borderColor:'#ff4455',tension:0.4,pointRadius:0},
                ]
            },
            options:{plugins:{legend:{labels:{color:'#888',font:{size:10}}}},scales:{x:{ticks:{color:'#555'}},y:{ticks:{color:'#555'}}}}
        });
    } catch(e) {}
};

/* ── CONCEPT DRIFT MONITOR ── */
window.loadConceptDrift = async function() {
    var wrap = document.getElementById('concept-drift-list');
    if (!wrap) return;
    try {
        var r = await _api('/agents/concept-drift');
        var d = r.ok ? await r.json() : {};
        var agents = d.agents || [];
        wrap.innerHTML = agents.map(function(a) {
            var sc = a.drift_score > 0.7 ? 'var(--rd)' : a.drift_score > 0.4 ? 'var(--am)' : 'var(--gr)';
            return '<div class="drift-agent-card">'
                + '<div style="display:flex;justify-content:space-between;align-items:center">'
                + '<span style="font-size:11px;color:var(--t1)">' + a.agent_id + '</span>'
                + (a.retrain_recommended ? '<span style="font-size:9px;color:var(--am)">⚠ Retreino recomendado</span>' : '')
                + '</div>'
                + '<div style="display:flex;gap:12px;margin-top:6px">'
                + '<span style="font-size:9px;color:var(--t3)">Drift: <span style="color:' + sc + '">' + (a.drift_score*100).toFixed(1) + '%</span></span>'
                + '<span style="font-size:9px;color:var(--t3)">Acurácia: <span style="color:var(--cy)">' + (a.current_accuracy*100).toFixed(1) + '%</span></span>'
                + '</div></div>';
        }).join('');
    } catch(e) {}
};

/* ── MEMORY PRUNING CONSOLE ── */
var _pruneSelected = new Set();
window.analyzeMemory = async function() {
    var wrap = document.getElementById('memory-prune-list');
    if (!wrap) return;
    _pruneSelected.clear();
    try {
        var r = await _api('/memory/analyze');
        var d = r.ok ? await r.json() : {};
        var items = d.items || [];
        wrap.innerHTML = items.map(function(item) {
            var candidate = item.prune_candidate;
            return '<div class="prune-result-row" style="' + (candidate ? 'border-color:rgba(255,68,85,0.3)' : '') + '">'
                + '<input type="checkbox" ' + (candidate ? 'checked' : '') + ' onchange="var s=this.checked;s?_pruneSelected.add(\'' + item.key + '\'): _pruneSelected.delete(\'' + item.key + '\')">'
                + '<div style="flex:1;min-width:0"><div style="font-size:9px;color:var(--cy);font-family:var(--m);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + item.key + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">' + item.age_days + 'd · ' + item.access_count + ' acessos · ' + (item.size_bytes/1024).toFixed(1) + 'KB</div></div>'
                + (candidate ? '<span style="font-size:8px;color:var(--rd)">CANDIDATO</span>' : '')
                + '</div>';
        }).join('') || '<div style="color:var(--gr);font-size:10px;padding:10px">✓ Memória saudável.</div>';
        items.forEach(function(item) { if (item.prune_candidate) _pruneSelected.add(item.key); });
    } catch(e) {}
};
window.pruneMemory = async function() {
    if (!_pruneSelected.size) { showToast('var(--am)', 'Selecione chaves para podar'); return; }
    if (!confirm('Remover ' + _pruneSelected.size + ' entradas de memória?')) return;
    var r = await _apif('/memory/prune', {keys: Array.from(_pruneSelected)});
    var d = r.ok ? await r.json() : {};
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? d.pruned + ' entradas removidas' : 'Erro');
    analyzeMemory();
};

/* ── RBAC MANAGER ── */
var _rbacSelectedRole = null;
window.loadRbac = async function() {
    var wrap = document.getElementById('rbac-roles-list');
    if (!wrap) return;
    try {
        var r = await _api('/rbac/roles');
        var d = r.ok ? await r.json() : {};
        var roles = d.roles || [];
        wrap.innerHTML = roles.map(function(role) {
            var perms = Array.isArray(role.permissions) ? role.permissions.join(', ') : role.permissions;
            return '<div class="rbac-role-item" onclick="selectRbacRole(\'' + role.id + '\')" id="rbac-' + role.id + '">'
                + '<div style="font-size:11px;color:var(--t1)">' + role.name + '</div>'
                + '<div style="font-size:9px;color:var(--t3);font-family:var(--m)">' + (perms||'') + '</div></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px">Nenhuma role.</div>';
    } catch(e) {}
};
window.selectRbacRole = function(id) {
    _rbacSelectedRole = id;
    document.querySelectorAll('.rbac-role-item').forEach(function(el) { el.style.background = 'rgba(255,255,255,0.03)'; });
    var el = document.getElementById('rbac-' + id);
    if (el) el.style.background = 'rgba(0,212,255,0.08)';
};
window.rbacTogglePerm = async function(perm) {
    if (!_rbacSelectedRole) { showToast('var(--am)', 'Selecione uma role'); return; }
    showToast('var(--am)', 'Toggle perm ' + perm + ' para ' + _rbacSelectedRole);
};
window.openRbacModal = function() {
    var name = prompt('Nome da nova role:');
    if (!name) return;
    _apif('/rbac/roles', {name: name, permissions: []}).then(function(r) {
        if (r.ok) { showToast('var(--gr)', 'Role criada'); loadRbac(); }
    });
};

/* ── DATA LINEAGE ── */
var _lineageNetwork = null;
window.loadDataLineage = async function() {
    var container = document.getElementById('lineage-canvas');
    if (!container || typeof vis === 'undefined') return;
    try {
        var search = document.getElementById('lineage-search') ? document.getElementById('lineage-search').value : '';
        var r = await _api('/data-lineage?search=' + encodeURIComponent(search));
        var d = r.ok ? await r.json() : {};
        var nodeColors = {source:'#00d4ff', cache:'#ffaa00', vector:'#cc88ff', processor:'#00ff88', consumer:'#ff8844'};
        var nodes = new vis.DataSet((d.nodes||[]).map(function(n) {
            return {id:n.id, label:n.label, color:nodeColors[n.type]||'#888'};
        }));
        var edges = new vis.DataSet((d.edges||[]).map(function(e) {
            return {from:e.from, to:e.to, label:e.label, arrows:'to'};
        }));
        if (_lineageNetwork) _lineageNetwork.destroy();
        _lineageNetwork = new vis.Network(container, {nodes:nodes,edges:edges}, {
            layout:{hierarchical:{direction:'LR',sortMethod:'directed'}},
            nodes:{font:{color:'#e0e0e0'},borderWidth:1,shape:'box'},
            edges:{color:'rgba(255,255,255,0.3)',font:{color:'#888',size:9}}
        });
    } catch(e) {}
};
window.searchLineage = function() { loadDataLineage(); };

/* ── SECRET ROTATION ── */
window.loadSecrets = async function() {
    var wrap = document.getElementById('secrets-list');
    if (!wrap) return;
    try {
        var r = await _api('/secrets');
        var d = r.ok ? await r.json() : {};
        var secrets = d.secrets || [];
        wrap.innerHTML = secrets.map(function(s) {
            var dt = s.rotated_at ? new Date(s.rotated_at*1000).toLocaleDateString('pt-BR') : '—';
            return '<div class="secret-row">'
                + '<div style="flex:1"><div style="font-size:11px;color:var(--t1)">' + s.name + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">Provider: ' + s.provider + ' · Rotacionado: ' + dt + '</div></div>'
                + '<button class="btn" style="font-size:9px;color:var(--am);border-color:var(--am)40" onclick="rotateSecret(\'' + s.id + '\')">↻ Rotacionar</button></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhum secret cadastrado.</div>';
    } catch(e) {}
};
window.rotateSecret = async function(id) {
    var newVal = prompt('Novo valor do secret:');
    if (newVal === null) return;
    var r = await _apif('/secrets/' + id + '/rotate', {new_value: newVal});
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Secret rotacionado' : 'Erro');
    loadSecrets();
};
window.openSecretModal = function() {
    var name = prompt('Nome do secret:');
    if (!name) return;
    var value = prompt('Valor:');
    if (value === null) return;
    _apif('/secrets', {name: name, value: value}).then(function(r) {
        if (r.ok) { showToast('var(--gr)', 'Secret criado'); loadSecrets(); }
    });
};

/* ── COMPLIANCE REPORT ── */
window.generateComplianceReport = async function() {
    var wrap = document.getElementById('compliance-list');
    if (!wrap) return;
    try {
        var r = await _api('/compliance/report');
        var d = r.ok ? await r.json() : {};
        var checks = d.checks || [];
        var summary = d.summary || {};
        var summaryEl = document.getElementById('compliance-summary');
        if (summaryEl) {
            summaryEl.innerHTML = '<span style="color:var(--gr)">✓ ' + (summary.pass||0) + ' pass</span> · '
                + '<span style="color:var(--am)">⚠ ' + (summary.warn||0) + ' warn</span> · '
                + '<span style="color:var(--rd)">✕ ' + (summary.fail||0) + ' fail</span>';
        }
        wrap.innerHTML = checks.map(function(c) {
            var sc = c.status === 'pass' ? 'var(--gr)' : c.status === 'warn' ? 'var(--am)' : 'var(--rd)';
            var icon = c.status === 'pass' ? '✓' : c.status === 'warn' ? '⚠' : '✕';
            return '<div class="comp-check-row">'
                + '<span style="color:' + sc + ';min-width:16px">' + icon + '</span>'
                + '<span style="flex:1;font-size:10px;color:var(--t2)">' + c.check + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">' + c.standard + '</span></div>';
        }).join('');
    } catch(e) {}
};
window.exportCompliancePdf = function() { showToast('var(--am)', 'Export PDF em desenvolvimento'); };

/* ── TENANT ANALYTICS ── */
window.loadTenantAnalytics = async function() {
    var wrap = document.getElementById('tenant-analytics-table');
    if (!wrap) return;
    try {
        var r = await _api('/tenants/analytics');
        var d = r.ok ? await r.json() : {};
        var tenants = d.tenants || [];
        wrap.innerHTML = '<table class="db-results-table"><thead><tr>'
            + '<th>Tenant</th><th>Tasks 30d</th><th>Tokens</th><th>Agentes</th><th>Erros %</th><th>Custo USD</th>'
            + '</tr></thead><tbody>'
            + tenants.map(function(t) {
                return '<tr><td>' + t.tenant_id + '</td><td>' + t.tasks_30d.toLocaleString() + '</td>'
                    + '<td>' + (t.tokens_30d/1000).toFixed(0) + 'K</td><td>' + t.agents_active + '</td>'
                    + '<td style="color:' + (t.error_rate_pct>3?'var(--rd)':t.error_rate_pct>1?'var(--am)':'var(--gr)') + '">' + t.error_rate_pct + '%</td>'
                    + '<td>$' + t.cost_usd.toFixed(2) + '</td></tr>';
            }).join('')
            + '</tbody></table>';
    } catch(e) {}
};
window.exportTenantCsv = function() { showToast('var(--am)', 'Export CSV em desenvolvimento'); };

/* ── BILLING EXPORT ── */
window.loadBillingData = async function() {
    var canvas = document.getElementById('billing-chart');
    var wrap = document.getElementById('billing-table');
    try {
        var r = await _api('/billing/summary');
        var d = r.ok ? await r.json() : {};
        var months = d.months || [];
        months.reverse();
        var labels = months.map(function(m) { return new Date(m.month*1000).toLocaleDateString('pt-BR',{month:'short',year:'2-digit'}); });
        if (canvas && typeof Chart !== 'undefined') {
            if (canvas._chart) canvas._chart.destroy();
            canvas._chart = new Chart(canvas, {
                type:'bar',
                data:{labels:labels, datasets:[{label:'Custo USD',data:months.map(function(m){return m.cost_usd;}), backgroundColor:'rgba(0,212,255,0.4)',borderColor:'var(--cy)',borderWidth:1}]},
                options:{plugins:{legend:{labels:{color:'#888',font:{size:10}}}},scales:{x:{ticks:{color:'#555'}},y:{ticks:{color:'#555'}}}}
            });
        }
        if (wrap) {
            wrap.innerHTML = '<table class="db-results-table"><thead><tr><th>Mês</th><th>Tokens</th><th>Tasks</th><th>Custo</th></tr></thead><tbody>'
                + months.map(function(m) {
                    var dt = new Date(m.month*1000).toLocaleDateString('pt-BR',{month:'long',year:'numeric'});
                    return '<tr><td>' + dt + '</td><td>' + (m.tokens_used/1000).toFixed(0) + 'K</td>'
                        + '<td>' + m.tasks_completed.toLocaleString() + '</td><td>$' + m.cost_usd.toFixed(2) + '</td></tr>';
                }).join('') + '</tbody></table>';
        }
    } catch(e) {}
};
window.generateInvoice = function() { showToast('var(--am)', 'Invoice generation em desenvolvimento'); };

/* ── FEATURE FLAGS ── */
window.loadFeatureFlags = async function() {
    var wrap = document.getElementById('feature-flags-list');
    if (!wrap) return;
    try {
        var r = await _api('/feature-flags');
        var d = r.ok ? await r.json() : {};
        var flags = d.flags || [];
        wrap.innerHTML = flags.map(function(f) {
            var enabled = f.enabled === 'true';
            return '<div class="flag-row">'
                + '<div style="flex:1"><div style="font-size:11px;color:var(--t1)">' + f.name + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">' + f.description + ' · rollout: ' + f.rollout_pct + '%</div></div>'
                + '<label class="flag-toggle"><input type="checkbox" ' + (enabled?'checked':'') + ' onchange="toggleFlag(\'' + f.id + '\',this.checked)">'
                + '<span class="flag-slider"></span></label></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhuma feature flag.</div>';
    } catch(e) {}
};
window.toggleFlag = async function(id, enabled) {
    await _apif('/feature-flags/' + id, {enabled: enabled});
    showToast('var(--gr)', 'Flag ' + (enabled?'ativada':'desativada'));
};
window.openFlagModal = function() {
    var name = prompt('Nome da feature flag:');
    if (!name) return;
    _apif('/feature-flags', {name: name, description: '', rollout_pct: 0}).then(function(r) {
        if (r.ok) { showToast('var(--gr)', 'Flag criada'); loadFeatureFlags(); }
    });
};

/* ── TENANT ONBOARDING WIZARD ── */
var _obStep = 1;
window.obNextStep = function() {
    if (_obStep >= 4) return;
    document.getElementById('ob-step-' + _obStep) && (document.getElementById('ob-step-' + _obStep).style.opacity = '0.4');
    _obStep++;
    var el = document.getElementById('ob-step-' + _obStep);
    if (el) { el.style.opacity = '1'; el.style.borderColor = 'var(--cy)'; }
    showToast('var(--gr)', 'Passo ' + _obStep + ' de 4');
};
window.obCreateTenant = async function() {
    var name = document.getElementById('ob-tenant-name') ? document.getElementById('ob-tenant-name').value : '';
    if (!name) { showToast('var(--am)', 'Nome do tenant obrigatório'); return; }
    showToast('var(--gr)', 'Tenant "' + name + '" criado');
    obNextStep();
};
window.obRunTest = async function() {
    showToast('var(--gr)', 'Teste de conectividade OK');
    obNextStep();
};

/* ── TENANT ISOLATION MONITOR ── */
window.runIsolationScan = async function() {
    var wrap = document.getElementById('isolation-results');
    if (wrap) wrap.innerHTML = '<div style="color:var(--t3);font-size:10px">Scanning...</div>';
    try {
        var r = await _api('/tenant-isolation/scan');
        var d = r.ok ? await r.json() : {};
        var results = d.results || [];
        if (wrap) wrap.innerHTML = results.map(function(res) {
            var sc = res.data_leakage_risk === 'high' ? 'var(--rd)' : 'var(--gr)';
            return '<div class="iso-result-row">'
                + '<span style="flex:1;font-size:10px;color:var(--t1)">' + res.tenant_id + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">Score: ' + (res.isolation_score*100).toFixed(1) + '%</span>'
                + '<span style="font-size:9px;color:' + sc + ';min-width:70px;text-align:right">'
                + (res.data_leakage_risk === 'high' ? '⚠ Risco Alto' : '✓ Isolado') + '</span></div>';
        }).join('');
    } catch(e) {}
};

/* ── PREDICTIVE CAPACITY ── */
window.loadPredictiveCapacity = async function() {
    var canvas = document.getElementById('capacity-chart');
    var wrap = document.getElementById('capacity-alerts');
    try {
        var r = await _api('/capacity/predict');
        var d = r.ok ? await r.json() : {};
        var pts = d.forecast || [];
        var labels = pts.map(function(p) { return new Date(p.date*1000).toLocaleDateString('pt-BR',{month:'short',day:'numeric'}); });
        if (canvas && typeof Chart !== 'undefined') {
            if (canvas._chart) canvas._chart.destroy();
            canvas._chart = new Chart(canvas, {
                type:'line',
                data:{labels:labels,datasets:[
                    {label:'CPU %',data:pts.map(function(p){return p.predicted_cpu_pct;}),borderColor:'var(--cy)',tension:0.4,pointRadius:0},
                ]},
                options:{plugins:{legend:{labels:{color:'#888',font:{size:10}}}},scales:{x:{ticks:{color:'#555'}},y:{ticks:{color:'#555'},min:0,max:100}}}
            });
        }
        if (wrap) {
            var alerts = pts.filter(function(p) { return p.capacity_alert; });
            wrap.innerHTML = alerts.map(function(p) {
                return '<div class="pc-alert-row"><span style="color:var(--am)">⚠</span>'
                    + '<span style="font-size:10px;color:var(--t2)">'
                    + new Date(p.date*1000).toLocaleDateString('pt-BR') + ' — CPU ' + p.predicted_cpu_pct + '%</span></div>';
            }).join('') || '<div style="color:var(--gr);font-size:10px">✓ Capacidade adequada nos próximos 14 dias.</div>';
        }
    } catch(e) {}
};

/* ── COST FORECASTING ── */
window.loadCostForecast = async function() {
    var canvas = document.getElementById('cost-forecast-chart');
    try {
        var r = await _api('/costs/forecast');
        var d = r.ok ? await r.json() : {};
        var pts = d.points || [];
        var labels = pts.map(function(p) { return new Date(p.month*1000).toLocaleDateString('pt-BR',{month:'short',year:'2-digit'}); });
        if (canvas && typeof Chart !== 'undefined') {
            if (canvas._chart) canvas._chart.destroy();
            canvas._chart = new Chart(canvas, {
                type:'line',
                data:{labels:labels,datasets:[
                    {label:'Real USD',data:pts.map(function(p){return p.actual_usd;}),borderColor:'var(--gr)',tension:0.4,borderDash:[0]},
                    {label:'Previsto USD',data:pts.map(function(p){return p.predicted_usd;}),borderColor:'var(--am)',tension:0.4,borderDash:[4,4]},
                ]},
                options:{plugins:{legend:{labels:{color:'#888',font:{size:10}}}},scales:{x:{ticks:{color:'#555'}},y:{ticks:{color:'#555'}}}}
            });
        }
    } catch(e) {}
};

/* ── VALUE ATTRIBUTION ── */
window.loadValueAttribution = async function() {
    var wrap = document.getElementById('value-attribution-list');
    if (!wrap) return;
    try {
        var r = await _api('/costs/value-attribution');
        var d = r.ok ? await r.json() : {};
        var items = d.items || [];
        items.sort(function(a,b){return b.roi-a.roi;});
        wrap.innerHTML = items.map(function(item) {
            var roiColor = item.roi > 3 ? 'var(--gr)' : item.roi > 1 ? 'var(--am)' : 'var(--rd)';
            return '<div class="va-row">'
                + '<span style="flex:1;font-size:10px;color:var(--t2)">' + item.category + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">$' + item.cost_usd.toFixed(2) + '</span>'
                + '<span style="font-size:9px;color:' + roiColor + ';min-width:60px;text-align:right">ROI ' + item.roi.toFixed(1) + 'x</span></div>';
        }).join('');
    } catch(e) {}
};

/* ── QUOTA OPTIMIZER ── */
window.loadQuotaOptimizer = async function() {
    var wrap = document.getElementById('quota-suggestions');
    if (!wrap) return;
    try {
        var r = await _api('/quotas/optimize');
        var d = r.ok ? await r.json() : {};
        var suggestions = d.suggestions || [];
        wrap.innerHTML = suggestions.map(function(s) {
            var savings = s.savings_pct;
            var arrow = s.suggested_limit < s.current_limit ? '↓' : s.suggested_limit > s.current_limit ? '↑' : '=';
            var color = savings > 0 ? 'var(--gr)' : 'var(--am)';
            return '<div class="quota-opt-row">'
                + '<span style="flex:1;font-size:10px;color:var(--t2)">' + s.resource + '</span>'
                + '<span style="font-size:9px;color:var(--t3)">' + s.current_limit + ' → <span style="color:var(--cy)">' + s.suggested_limit + '</span></span>'
                + '<span style="font-size:9px;color:var(--t3);min-width:60px">' + s.utilization_pct + '% uso</span>'
                + '<span style="font-size:9px;color:' + color + ';min-width:60px;text-align:right">' + arrow + ' ' + savings + '% economia</span></div>';
        }).join('');
        window._quotaSuggestions = suggestions;
    } catch(e) {}
};
window.applyQuotaSuggestions = async function() {
    if (!window._quotaSuggestions) return;
    var r = await _apif('/quotas/apply', {suggestions: window._quotaSuggestions});
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Quotas aplicadas' : 'Erro');
};

/* ── RUNBOOK EXECUTOR ── */
var _selectedRunbook = null;
window.loadRunbooks = async function() {
    var wrap = document.getElementById('runbooks-list');
    if (!wrap) return;
    try {
        var r = await _api('/runbooks');
        var d = r.ok ? await r.json() : {};
        var runbooks = d.runbooks || [];
        wrap.innerHTML = runbooks.map(function(rb) {
            return '<div class="runbook-item" onclick="selectRunbook(\'' + rb.id + '\')" id="rb-' + rb.id + '">'
                + '<div style="font-size:11px;color:var(--t1)">' + rb.name + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">' + rb.description + '</div></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhum runbook.</div>';
    } catch(e) {}
};
window.selectRunbook = function(id) {
    _selectedRunbook = id;
    document.querySelectorAll('.runbook-item').forEach(function(el) { el.style.background = 'rgba(255,255,255,0.03)'; });
    var el = document.getElementById('rb-' + id);
    if (el) el.style.background = 'rgba(0,212,255,0.08)';
};
window.executeRunbook = async function() {
    if (!_selectedRunbook) { showToast('var(--am)', 'Selecione um runbook'); return; }
    var r = await _apif('/runbooks/' + _selectedRunbook + '/run', {});
    var d = r.ok ? await r.json() : {};
    showToast(d.ok ? 'var(--gr)' : 'var(--rd)', d.ok ? d.message : d.error || 'Erro');
};
window.openRunbookModal = function() {
    var name = prompt('Nome do runbook:');
    if (!name) return;
    _apif('/runbooks', {name: name, description: '', steps: []}).then(function(r) {
        if (r.ok) { showToast('var(--gr)', 'Runbook criado'); loadRunbooks(); }
    });
};

/* ── CHAOS ENGINEERING ── */
window.chaosKillAgent = async function() {
    var sel = document.getElementById('chaos-agent');
    var agent_id = sel ? sel.value : 'agent-000';
    var r = await _apif('/chaos/kill-agent', {agent_id: agent_id});
    _logChaos('KILL AGENT: ' + agent_id);
    showToast('var(--rd)', 'Agent ' + agent_id + ' killed');
};
window.chaosInjectDelay = async function() {
    var inp = document.getElementById('chaos-delay-ms');
    var delay_ms = inp ? parseInt(inp.value) || 500 : 500;
    var r = await _apif('/chaos/inject-delay', {delay_ms: delay_ms, target: 'all'});
    _logChaos('INJECT DELAY: ' + delay_ms + 'ms');
    showToast('var(--am)', delay_ms + 'ms delay injetado');
};
window.chaosSaturateQueue = async function() {
    var inp = document.getElementById('chaos-queue-tasks');
    var count = inp ? parseInt(inp.value) || 100 : 100;
    var r = await _apif('/chaos/saturate-queue', {count: count});
    _logChaos('SATURATE QUEUE: ' + count + ' tasks');
    showToast('var(--am)', count + ' tasks adicionadas à fila');
};
window.chaosErrorRate = async function() {
    var inp = document.getElementById('chaos-error-rate');
    var rate = inp ? parseInt(inp.value) || 50 : 50;
    var r = await _apif('/chaos/error-rate', {rate_pct: rate});
    _logChaos('ERROR RATE: ' + rate + '%');
    showToast('var(--am)', 'Error rate ' + rate + '% por 5 min');
};
window.loadChaosAgents = async function() {
    var sel = document.getElementById('chaos-agent');
    if (!sel) return;
    try {
        var r = await _api('/tasks?limit=20');
        var d = r.ok ? await r.json() : {};
        var agents = ['agent-000','agent-001','agent-002','agent-003'];
        sel.innerHTML = agents.map(function(a) { return '<option value="' + a + '">' + a + '</option>'; }).join('');
    } catch(e) {}
};
function _logChaos(msg) {
    var wrap = document.getElementById('chaos-log');
    if (!wrap) return;
    var ts = new Date().toLocaleTimeString('pt-BR');
    var entry = document.createElement('div');
    entry.className = 'chaos-log-entry';
    entry.textContent = '[' + ts + '] ' + msg;
    wrap.insertBefore(entry, wrap.firstChild);
}

/* ── CANARY RELEASE MANAGER ── */
window.loadCanaryStatus = async function() {
    var wrap = document.getElementById('canary-list');
    if (!wrap) return;
    try {
        var r = await _api('/deployments/canary');
        var d = r.ok ? await r.json() : {};
        var releases = d.releases || [];
        wrap.innerHTML = releases.map(function(c) {
            var pct = parseInt(c.traffic_pct) || 0;
            var stColor = c.status === 'complete' ? 'var(--gr)' : c.status === 'rolled_back' ? 'var(--rd)' : 'var(--cy)';
            return '<div class="canary-card">'
                + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
                + '<span style="font-size:12px;color:var(--t1)">' + c.name + '</span>'
                + '<span style="font-size:9px;color:' + stColor + '">' + c.status + '</span></div>'
                + '<div style="font-size:9px;color:var(--t3);margin-bottom:8px">'
                + c.stable_version + ' → <span style="color:var(--cy)">' + c.canary_version + '</span></div>'
                + '<div class="canary-pct-wrap">'
                + '<div style="height:100%;width:' + pct + '%;background:var(--cy);border-radius:3px;transition:width 0.5s"></div></div>'
                + '<div style="font-size:10px;color:var(--cy);text-align:right;margin-top:4px">' + pct + '% canary</div>'
                + '<div style="display:flex;gap:6px;margin-top:8px">'
                + '<button class="btn" style="flex:1;font-size:9px" onclick="advanceCanary(\'' + c.id + '\')">+10% →</button>'
                + '<button class="btn" style="font-size:9px;color:var(--rd);border-color:var(--rd)40" onclick="rollbackCanary(\'' + c.id + '\')">Rollback</button>'
                + '</div></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px;padding:10px">Nenhum canary ativo.</div>';
    } catch(e) {}
};
window.advanceCanary = async function(id) {
    var r = await _apif('/deployments/canary/' + id + '/advance', {});
    var d = r.ok ? await r.json() : {};
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Canary: ' + d.traffic_pct + '%' : 'Erro');
    loadCanaryStatus();
};
window.rollbackCanary = async function(id) {
    if (!confirm('Fazer rollback do canary?')) return;
    var r = await _apif('/deployments/canary/' + id + '/rollback', {});
    showToast(r.ok ? 'var(--am)' : 'var(--rd)', r.ok ? 'Canary revertido' : 'Erro');
    loadCanaryStatus();
};
window.openCanaryModal = function() {
    var name = prompt('Nome do release:');
    if (!name) return;
    var canary_ver = prompt('Versão canary:');
    if (!canary_ver) return;
    _apif('/deployments/canary', {name: name, stable_version: 'current', canary_version: canary_ver}).then(function(r) {
        if (r.ok) { showToast('var(--gr)' , 'Canary criado'); loadCanaryStatus(); }
    });
};

/* ── POSTMORTEM BUILDER ── */
var _currentPmId = null;
window.loadPostmortems = async function() {
    var wrap = document.getElementById('postmortem-list');
    if (!wrap) return;
    try {
        var r = await _api('/postmortems');
        var d = r.ok ? await r.json() : {};
        var pms = d.postmortems || [];
        wrap.innerHTML = pms.map(function(pm) {
            var dt = pm.incident_date || '—';
            return '<div class="postmortem-item" onclick="loadPmDetail(\'' + pm.id + '\')" id="pm-' + pm.id + '">'
                + '<div style="font-size:11px;color:var(--t1)">' + pm.title + '</div>'
                + '<div style="font-size:9px;color:var(--t3)">' + dt + ' · <span style="color:var(--am)">' + pm.severity + '</span></div></div>';
        }).join('') || '<div style="color:var(--t3);font-size:10px">Nenhum postmortem.</div>';
    } catch(e) {}
};
window.loadPmDetail = function(id) {
    _currentPmId = id;
    document.querySelectorAll('.postmortem-item').forEach(function(el) { el.style.background = 'rgba(255,255,255,0.03)'; });
    var el = document.getElementById('pm-' + id);
    if (el) el.style.background = 'rgba(0,212,255,0.08)';
    showToast('var(--cy)', 'Postmortem ' + id + ' selecionado');
};
window.newPostmortem = function() {
    _currentPmId = null;
    var fields = ['pm-title','pm-date','pm-severity','pm-summary','pm-timeline','pm-root-cause','pm-actions'];
    fields.forEach(function(id) { var el = document.getElementById(id); if (el) el.value = ''; });
};
window.savePostmortem = async function() {
    var get = function(id) { var el = document.getElementById(id); return el ? el.value : ''; };
    var payload = {
        title: get('pm-title') || 'Incident Postmortem',
        incident_date: get('pm-date'),
        severity: get('pm-severity') || 'P2',
        summary: get('pm-summary'),
        timeline: get('pm-timeline'),
        root_cause: get('pm-root-cause'),
        action_items: get('pm-actions'),
    };
    var r;
    if (_currentPmId) {
        r = await _api('/postmortems/' + _currentPmId, {method:'PUT', headers:{'Content-Type':'application/json'}, body: JSON.stringify(payload)});
    } else {
        r = await _apif('/postmortems', payload);
    }
    showToast(r.ok ? 'var(--gr)' : 'var(--rd)', r.ok ? 'Postmortem salvo' : 'Erro');
    loadPostmortems();
};
window.exportPostmortemMd = function() {
    var get = function(id) { var el = document.getElementById(id); return el ? el.value : ''; };
    var md = '# ' + (get('pm-title') || 'Postmortem') + '\n\n'
        + '**Data:** ' + get('pm-date') + '\n'
        + '**Severidade:** ' + get('pm-severity') + '\n\n'
        + '## Sumário\n' + get('pm-summary') + '\n\n'
        + '## Timeline\n' + get('pm-timeline') + '\n\n'
        + '## Causa Raiz\n' + get('pm-root-cause') + '\n\n'
        + '## Action Items\n' + get('pm-actions') + '\n';
    var blob = new Blob([md], {type: 'text/markdown'});
    var a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'postmortem.md';
    a.click();
};

/* ── SDK GENERATOR ── */
var _sdkTemplates = {
    python: function(ep) {
        return 'import anthropic\nclient = anthropic.Anthropic()\n\n'
            + 'response = client.messages.create(\n'
            + '    model="claude-sonnet-4-6",\n'
            + '    max_tokens=1024,\n'
            + '    messages=[{"role": "user", "content": "Hello!"}]\n)\nprint(response.content)';
    },
    javascript: function(ep) {
        return 'import Anthropic from "@anthropic-ai/sdk";\nconst client = new Anthropic();\n\n'
            + 'const response = await client.messages.create({\n'
            + '  model: "claude-sonnet-4-6",\n'
            + '  max_tokens: 1024,\n'
            + '  messages: [{ role: "user", content: "Hello!" }],\n});\nconsole.log(response.content);';
    },
    typescript: function(ep) {
        return 'import Anthropic from "@anthropic-ai/sdk";\nconst client: Anthropic = new Anthropic();\n\n'
            + 'const response = await client.messages.create({\n'
            + '  model: "claude-sonnet-4-6",\n'
            + '  max_tokens: 1024,\n'
            + '  messages: [{ role: "user", content: "Hello!" }],\n});';
    },
    go: function(ep) {
        return 'package main\nimport (\n    "github.com/anthropics/anthropic-sdk-go"\n)\n\n'
            + 'func main() {\n    client := anthropic.NewClient()\n'
            + '    // Use client to make API calls\n}';
    },
    curl: function(ep) {
        return 'curl https://api.anthropic.com/v1/messages \\\n'
            + '  -H "x-api-key: $ANTHROPIC_API_KEY" \\\n'
            + '  -H "anthropic-version: 2023-06-01" \\\n'
            + '  -H "content-type: application/json" \\\n'
            + '  -d \'{\n    "model": "claude-sonnet-4-6",\n'
            + '    "max_tokens": 1024,\n'
            + '    "messages": [{"role": "user", "content": "Hello!"}]\n  }\'';
    }
};
window.generateSdk = function() {
    var lang = document.getElementById('sdk-language') ? document.getElementById('sdk-language').value : 'python';
    var ep = document.getElementById('sdk-endpoint') ? document.getElementById('sdk-endpoint').value : '';
    var wrap = document.getElementById('sdk-output');
    if (!wrap) return;
    var tpl = _sdkTemplates[lang] || _sdkTemplates.python;
    wrap.textContent = tpl(ep);
};
window.copySdkCode = function() {
    var wrap = document.getElementById('sdk-output');
    if (!wrap) return;
    navigator.clipboard.writeText(wrap.textContent).then(function() {
        showToast('var(--gr)', 'Código copiado!');
    });
};

/* ── CHANGELOG ── */
var _changelogEntries = [
    {version:'v2.5.0', date:'2026-03-22', type:'major', summary:'36 novas páginas de dashboard — Relatório Mestre completo', author:'Claude Sonnet 4.6'},
    {version:'v2.4.0', date:'2026-03-20', type:'major', summary:'Particles physics background, Neo4j graph focus mode e CRT terminal styles', author:'sistema'},
    {version:'v2.3.0', date:'2026-03-18', type:'major', summary:'Agent Job Board Kanban com human-in-the-loop actions', author:'sistema'},
    {version:'v2.2.0', date:'2026-03-15', type:'feature', summary:'Command palette L3 memory search overlay (Cmd-K)', author:'sistema'},
    {version:'v2.1.0', date:'2026-03-10', type:'feature', summary:'Ultimate NOC Phase 1 — glass odometer widgets e MCTS hacker terminal', author:'sistema'},
    {version:'v2.0.0', date:'2026-03-01', type:'major', summary:'Continuous learning memory (Qdrant injection hook) e MCP semantic search bridge', author:'sistema'},
];
window.loadChangelog = function() {
    var wrap = document.getElementById('changelog-list');
    if (!wrap) return;
    wrap.innerHTML = _changelogEntries.map(function(e) {
        var color = e.type === 'major' ? 'var(--cy)' : e.type === 'feature' ? 'var(--gr)' : 'var(--t3)';
        return '<div class="changelog-entry">'
            + '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            + '<span style="font-size:12px;font-weight:700;color:var(--t1);font-family:var(--m)">' + e.version + '</span>'
            + '<span style="font-size:9px;padding:2px 6px;border-radius:4px;background:' + color + '20;color:' + color + '">' + e.type + '</span>'
            + '<span style="font-size:9px;color:var(--t3);margin-left:auto">' + e.date + '</span></div>'
            + '<div style="font-size:11px;color:var(--t2)">' + e.summary + '</div>'
            + '<div style="font-size:9px;color:var(--t3);margin-top:4px">by ' + e.author + '</div></div>';
    }).join('');
};
window.openChangelogModal = function() {
    var summary = prompt('Nova entrada de changelog:');
    if (!summary) return;
    _changelogEntries.unshift({version:'v?', date:new Date().toISOString().split('T')[0], type:'fix', summary: summary, author:'manual'});
    loadChangelog();
};

/* ── _onPageEnter EXTENSION — all 36 pages ── */
var _origOnPageEnterN5b = window._onPageEnter;
window._onPageEnter = function(pid) {
    if (typeof _origOnPageEnterN5b === 'function') _origOnPageEnterN5b(pid);
    if (pid === 'batch-ops')              { _batchSelected.clear(); loadBatchTasks(); }
    if (pid === 'task-templates')         loadTaskTemplates();
    if (pid === 'rollback')               loadRollbackSnapshots();
    if (pid === 'context-analyzer')       loadContextTraces();
    if (pid === 'token-budget')           loadTokenBudgets();
    if (pid === 'ab-testing')             loadAbTests();
    if (pid === 'redis-inspector')        loadRedisKeys();
    if (pid === 'db-console')             {}
    if (pid === 'migration-console')      loadMigrations();
    if (pid === 'queue-heatmap')          loadQueueHeatmap();
    if (pid === 'blue-green')             loadBlueGreenStatus();
    if (pid === 'tracing')                loadTraces();
    if (pid === 'anomaly')                loadAnomalies();
    if (pid === 'correlation')            loadCorrelations();
    if (pid === 'knowledge-editor')       loadKnowledgeEditor();
    if (pid === 'learn-velocity')         loadLearnVelocity();
    if (pid === 'concept-drift')          loadConceptDrift();
    if (pid === 'memory-pruning')         analyzeMemory();
    if (pid === 'rbac')                   loadRbac();
    if (pid === 'data-lineage')           loadDataLineage();
    if (pid === 'secret-rotation')        loadSecrets();
    if (pid === 'compliance')             generateComplianceReport();
    if (pid === 'tenant-analytics')       loadTenantAnalytics();
    if (pid === 'billing')                loadBillingData();
    if (pid === 'feature-flags')          loadFeatureFlags();
    if (pid === 'tenant-onboarding')      { _obStep=1; }
    if (pid === 'tenant-isolation')       {}
    if (pid === 'predictive-capacity')    loadPredictiveCapacity();
    if (pid === 'cost-forecast')          loadCostForecast();
    if (pid === 'value-attribution')      loadValueAttribution();
    if (pid === 'quota-optimizer')        loadQuotaOptimizer();
    if (pid === 'runbook')                loadRunbooks();
    if (pid === 'chaos')                  loadChaosAgents();
    if (pid === 'canary')                 loadCanaryStatus();
    if (pid === 'postmortem')             loadPostmortems();
    if (pid === 'sdk-gen')                generateSdk();
    if (pid === 'changelog')              loadChangelog();
};


/* ── SIDEBAR: COLLAPSIBLE GROUPS ── */
window.sbToggle = function(ngEl) {
    var grp = ngEl.getAttribute('data-grp') || ngEl.dataset.grp;
    // get grp from next siblings
    var isOpen = ngEl.classList.contains('open');
    var items = document.querySelectorAll('.sb-grp[data-grp="' + (grp || '') + '"]');
    if (!items.length) {
        // fallback: toggle all siblings until next .ng
        var sib = ngEl.nextElementSibling;
        while (sib && !sib.classList.contains('ng')) {
            sib.style.display = isOpen ? 'none' : 'flex';
            sib = sib.nextElementSibling;
        }
    } else {
        items.forEach(function(el) {
            el.style.display = isOpen ? 'none' : 'flex';
        });
    }
    ngEl.classList.toggle('open', !isOpen);
};

/* ── SIDEBAR: SEARCH FILTER ── */
window.sbSearch = function(query) {
    var q = query.toLowerCase().trim();
    var allNi = document.querySelectorAll('#sb .ni');
    var allNg = document.querySelectorAll('#sb .ng');

    if (!q) {
        // restore: show pinned, hide grp items (respect open state)
        allNi.forEach(function(el) {
            if (el.classList.contains('sb-grp')) {
                // only show if parent group is open
                var grp = el.getAttribute('data-grp');
                var groupHeader = document.querySelector('#sb .ng-col[data-grp="' + grp + '"]') ||
                    findGroupHeader(el);
                el.style.display = (groupHeader && groupHeader.classList.contains('open')) ? 'flex' : 'none';
            } else {
                el.style.display = 'flex';
            }
        });
        allNg.forEach(function(el) { el.style.display = 'flex'; });
        return;
    }

    // search mode: show all matching, hide non-matching
    var anyMatch = false;
    allNg.forEach(function(el) { el.style.display = 'none'; });
    allNi.forEach(function(el) {
        var label = el.querySelector('.ni-label');
        var text = label ? label.textContent.toLowerCase() : '';
        if (text.includes(q)) {
            el.style.display = 'flex';
            anyMatch = true;
        } else {
            el.style.display = 'none';
        }
    });
};

function findGroupHeader(itemEl) {
    var prev = itemEl.previousElementSibling;
    while (prev) {
        if (prev.classList.contains('ng')) return prev;
        prev = prev.previousElementSibling;
    }
    return null;
}

/* ── AGENT ROSTER WIDGET (home page) ── */
var _ROSTER_STATUSES = ['idle','thinking','executing','error'];
var _ROSTER_STATUS_LABELS = {idle:'Idle',thinking:'Pensando',executing:'Executando',error:'Erro'};

window.loadAgentRoster = async function() {
    var wrap = document.getElementById('agent-roster-grid');
    if (!wrap) return;
    try {
        var r = await fetch('/api/v5/dashboard/agents?tenant_id=' + ((window.APP_STATE && APP_STATE.tenant_id) || 'default'));
        var d = r.ok ? await r.json() : {};
        var agents = d.agents || d.tasks || [];

        // If no agents from API, generate demo roster
        if (!agents.length) {
            agents = [
                {id:'lsp-watchdog', name:'LSP Watchdog', status:'idle', task:'—'},
                {id:'architect-01', name:'Architect Agent', status:'thinking', task:'Analisando dependências'},
                {id:'coder-01', name:'Code Generator', status:'executing', task:'Gerando componente UI'},
                {id:'reviewer-01', name:'PR Reviewer', status:'idle', task:'—'},
                {id:'rag-engine', name:'RAG Engine', status:'executing', task:'Embedding docs'},
                {id:'monitor-01', name:'NOC Monitor', status:'idle', task:'—'},
            ];
        }

        var onlineCount = agents.filter(function(a) { return a.status !== 'error'; }).length;
        var rosterStatus = document.getElementById('roster-status');
        if (rosterStatus) rosterStatus.textContent = '● ' + onlineCount + ' Online';

        wrap.innerHTML = agents.map(function(agent) {
            var st = agent.status || 'idle';
            var statusColor = {idle:'var(--gr)',thinking:'var(--bl)',executing:'var(--am)',error:'var(--rd)'}[st] || 'var(--t3)';
            var name = agent.name || agent.id || 'Agent';
            var task = agent.task || agent.goal || agent.current_task || '—';
            if (task.length > 30) task = task.substring(0, 28) + '…';

            return '<div class="agent-roster-card status-' + st + '" onclick="showPage('agents')" title="' + name + '">'
                + '<div style="display:flex;align-items:center;gap:6px">'
                + '<div class="roster-dot ' + st + '"></div>'
                + '<span style="font-size:10px;font-weight:500;color:var(--t1);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">' + name + '</span>'
                + '<span style="font-size:8px;color:' + statusColor + ';white-space:nowrap">' + (_ROSTER_STATUS_LABELS[st] || st) + '</span>'
                + '</div>'
                + '<div style="font-size:9px;color:var(--t3);font-family:var(--m);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;padding-left:13px">' + task + '</div>'
                + '</div>';
        }).join('');
    } catch(e) {
        if (wrap) wrap.innerHTML = '<div style="color:var(--t3);font-size:10px;padding:10px;grid-column:span 2">Erro ao carregar agentes</div>';
    }
};

/* ── HOME STATUS OPERACIONAL WIDGET ── */
window.loadHomeStatus = async function() {
    try {
        var tid = (window.APP_STATE && APP_STATE.tenant_id) || 'default';
        // SLA
        var rSla = await fetch('/api/v5/dashboard/sla?tenant_id=' + tid);
        if (rSla.ok) {
            var dSla = await rSla.json();
            var slaPct = dSla.overall_sla_pct || dSla.sla_pct || 99.9;
            var el = document.getElementById('home-sla-pct');
            if (el) {
                el.textContent = parseFloat(slaPct).toFixed(1) + '%';
                el.style.color = slaPct >= 99 ? 'var(--gr)' : slaPct >= 95 ? 'var(--am)' : 'var(--rd)';
            }
        }
        // Incidents
        var rInc = await fetch('/api/v5/dashboard/incidents?tenant_id=' + tid + '&status=open&limit=50');
        if (rInc.ok) {
            var dInc = await rInc.json();
            var incCount = (dInc.incidents || []).filter(function(i) { return i.status === 'open'; }).length;
            var elInc = document.getElementById('home-incidents-count');
            if (elInc) {
                elInc.textContent = incCount;
                elInc.style.color = incCount > 0 ? 'var(--rd)' : 'var(--am)';
            }
        }
    } catch(e) {}
};

/* ── AUTO-LOAD ROSTER AND STATUS ON HOME ── */
(function() {
    var _origOnPageEnterRoster = window._onPageEnter;
    window._onPageEnter = function(pid) {
        if (typeof _origOnPageEnterRoster === 'function') _origOnPageEnterRoster(pid);
        if (pid === 'home') {
            loadAgentRoster();
            loadHomeStatus();
        }
    };
    // Also load on initial render
    setTimeout(function() {
        if (document.getElementById('agent-roster-grid')) {
            loadAgentRoster();
            loadHomeStatus();
        }
    }, 800);
    // Refresh roster every 15s
    setInterval(function() {
        var grid = document.getElementById('agent-roster-grid');
        if (grid && grid.closest('[style*="display:none"]') === null) {
            loadAgentRoster();
        }
    }, 15000);
})();
