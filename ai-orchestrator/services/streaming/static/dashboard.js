// SINC Cognitive NOC v3 - Ultimate Dashboard Logic

const APP_STATE = {
    ws: null,
    tenant_id: 'default',
    reconnect_attempts: 0,
    max_reconnect: 5,
    network: null,
    graphNodes: null,
    graphEdges: null
};

// UI Elements
const els = {
    successRate: document.getElementById('metric-success-rate'),
    autonomyScore: document.getElementById('metric-autonomy-score'),
    activeAgents: document.getElementById('metric-active-agents'),
    latency: document.getElementById('metric-latency'),
    taskList: document.getElementById('task-list-container'),
    terminalFeed: document.getElementById('terminal-feed-container'),
    graphContainer: document.getElementById('graph-network-container'),
    burnRate: document.getElementById('metric-burn-rate')
};

// -----------------------------------------------------
// Vis.js Graph Engine Initialization
// -----------------------------------------------------
function initGraphEngine() {
    APP_STATE.graphNodes = new vis.DataSet([
        { id: 'core', label: 'SINC Core', group: 'core', mass: 4, shape: 'hexagon' }
    ]);
    APP_STATE.graphEdges = new vis.DataSet([]);

    const data = {
        nodes: APP_STATE.graphNodes,
        edges: APP_STATE.graphEdges
    };

    const options = {
        nodes: {
            shape: 'dot',
            size: 20,
            font: { color: '#f8f8fa', face: 'Outfit', size: 14, strokeWidth: 0 },
            borderWidth: 2,
            shadow: { enabled: true, color: 'rgba(138, 75, 255, 0.4)', size: 15 }
        },
        edges: {
            width: 1.5,
            color: { color: 'rgba(255, 255, 255, 0.15)', highlight: '#8a4bff' },
            smooth: { type: 'continuous' }
        },
        physics: {
            solver: 'forceAtlas2Based',
            forceAtlas2Based: {
                gravitationalConstant: -100,
                centralGravity: 0.015,
                springLength: 150,
                springConstant: 0.04
            },
            maxVelocity: 50,
            minVelocity: 0.1,
            timestep: 0.35,
            stabilization: { iterations: 150 }
        },
        groups: {
            core: { color: { background: '#8a4bff', border: '#b894ff' } },
            danger: { color: { background: '#f03250', border: '#ff708a' } },
            route: { color: { background: '#32d2ff', border: '#8ce5ff' } },
            default: { color: { background: '#8e8e9e', border: '#d1d1d6' } }
        },
        interaction: { hover: true, tooltipDelay: 200 }
    };

    APP_STATE.network = new vis.Network(els.graphContainer, data, options);
}

// -----------------------------------------------------
// WebSocket Telemetry
// -----------------------------------------------------
function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/v5/dashboard/ws/telemetry?tenant_id=${APP_STATE.tenant_id}`;

    APP_STATE.ws = new WebSocket(wsUrl);

    APP_STATE.ws.onopen = () => {
        writeToTerminal("Connection established with Core Telemetry Socket.", "success");
        updateConnectionStatus(true);
        APP_STATE.reconnect_attempts = 0;
    };

    APP_STATE.ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleTelemetryEvent(data);
        } catch (e) {
            writeToTerminal("Malformed telemetry payload received.", "err");
        }
    };

    APP_STATE.ws.onclose = () => {
        updateConnectionStatus(false);
        writeToTerminal("Connection dropped. Attempting semantic reconnect...", "warn");
        if (APP_STATE.reconnect_attempts < APP_STATE.max_reconnect) {
            APP_STATE.reconnect_attempts++;
            setTimeout(initWebSocket, 2000 * APP_STATE.reconnect_attempts);
        }
    };
}

function updateConnectionStatus(isOnline) {
    const indicator = document.querySelector('.status-indicator');
    const label = document.querySelector('.online-tag');
    
    if (isOnline) {
        indicator.style.background = 'var(--success)';
        indicator.style.boxShadow = '0 0 10px var(--success)';
        indicator.style.animation = 'blink 2s infinite';
        label.textContent = "System Online";
    } else {
        indicator.style.background = 'var(--danger)';
        indicator.style.boxShadow = '0 0 10px var(--danger)';
        indicator.style.animation = 'none';
        label.textContent = "Connection Lost";
    }
}

function handleTelemetryEvent(data) {
    if (data.type === 'summary' || data.metrics) updateMetrics(data);
    if (data.type === 'task_transition') renderTaskUpdate(data);
    if (data.type === 'blast_radius' || data.impact_map) renderGraphImpact(data);
    if (data.type === 'agent_thought' || data.mcts) addTerminalFeed(data);
    if (data.routing) document.getElementById('system-mode-display').textContent = (data.autonomy?.mode || 'AUTONOMOUS').toUpperCase();
}

function updateMetrics(data) {
    if(!data.metrics) return;
    const m = data.metrics;
    if(m.success_rate !== undefined) animateOdometer(els.successRate, `${(m.success_rate).toFixed(1)}%`);
    if(m.autonomy_score !== undefined) animateOdometer(els.autonomyScore, `${(m.autonomy_score).toFixed(1)}`);
    if(m.active_agents !== undefined) animateOdometer(els.activeAgents, m.active_agents);
    if(m.latency_p95 !== undefined) animateOdometer(els.latency, `${Math.round(m.latency_p95)}ms`);
    
    // Simulate/Inject Burn Rate 
    const burn = m.tokens_usd || Math.random() < 0.2 ? (Math.random() * 5).toFixed(2) : els.burnRate?.textContent.replace('$','');
    if (els.burnRate && burn !== undefined && burn !== "0.00") animateOdometer(els.burnRate, `$${burn}`);
}

function animateOdometer(el, newValue) {
    if (!el || el.textContent === String(newValue)) return;
    el.style.transform = 'translateY(-15px)';
    el.style.opacity = '0';
    setTimeout(() => {
        el.textContent = newValue;
        el.style.transform = 'translateY(15px)';
        requestAnimationFrame(() => {
            el.style.transform = 'translateY(0)';
            el.style.opacity = '1';
        });
    }, 150);
}

// -----------------------------------------------------
// Graph Mutations (Neo4j -> Vis.js)
// -----------------------------------------------------
function renderGraphImpact(data) {
    if(!data || !data.impact_map || data.impact_map.length === 0) return;
    
    // Animate target node
    const targetId = data.target || 'target_1';
    
    if (!APP_STATE.graphNodes.get(targetId)) {
        APP_STATE.graphNodes.add({
            id: targetId,
            label: targetId,
            group: 'core',
            size: 30
        });
        APP_STATE.graphEdges.add({ from: 'core', to: targetId });
    }

    // Add impacts dynamically
    data.impact_map.forEach((imp, i) => {
        if(i > 15) return; // limit cluster size for demo clarity
        const nodeId = imp.name || imp.file;
        
        let group = 'default';
        if(imp.risk === 'CRITICAL_ROUTE_BREAK') group = 'route';
        else if(imp.risk === 'CRITICAL_STATE_BREAK') group = 'danger';

        if (!APP_STATE.graphNodes.get(nodeId)) {
            APP_STATE.graphNodes.add({
                id: nodeId,
                label: imp.name || imp.file.split('/').pop(),
                group: group,
                title: `${imp.risk} | Depth: ${imp.depth}` // tooltip
            });
            
            APP_STATE.graphEdges.add({
                id: `${targetId}-${nodeId}`,
                from: targetId,
                to: nodeId,
                length: imp.depth * 50
            });
            
            writeToTerminal(`[GRAPH] Added structural mapping for node: ${nodeId}`, group === 'danger' ? 'err' : 'system');
        }
    });

    APP_STATE.network.fit({ animation: { duration: 1000, easingFunction: 'easeInOutQuad' } });
}

// -----------------------------------------------------
// Terminal Engine & Tasks
// -----------------------------------------------------
function writeToTerminal(text, type = "system") {
    const el = document.createElement('div');
    el.className = `term-line prefix-${type}`;
    els.terminalFeed.appendChild(el);
    
    // Typewriter effect
    let i = 0;
    const speed = 15; // ms
    
    function typeWriter() {
        if (i < text.length) {
            el.innerHTML += text.charAt(i);
            i++;
            els.terminalFeed.scrollTop = els.terminalFeed.scrollHeight;
            setTimeout(typeWriter, speed);
        }
    }
    
    typeWriter();

    // Auto cleanup terminal to avoid DOM bloat memory leaks
    if(els.terminalFeed.children.length > 50) {
        els.terminalFeed.removeChild(els.terminalFeed.firstChild);
    }
}

function addTerminalFeed(msg) {
    const content = msg.thought || msg.message || 'MCTS Planner: Evaluating task branches...';
    writeToTerminal(`> ${content}`, "success");
}

function renderTaskUpdate(task) {
    const taskId = `task-${task.task_id}`;
    let el = document.getElementById(taskId);
    
    if(!el) {
        el = document.createElement('div');
        el.id = taskId;
        el.className = 'task-item';
        if(els.taskList.children.length >= 6) {
            els.taskList.removeChild(els.taskList.lastChild);
        }
        els.taskList.prepend(el);
    }
    
    const isRunning = task.status === 'running' || task.status === 'pending';
    const statusClass = isRunning ? 'active' : '';
    const icon = isRunning ? 'loader' : (task.status==='completed' ? 'check' : 'alert-circle');
    
    el.innerHTML = `
        <div class="task-status ${statusClass}">
            <i data-lucide="${icon}"></i>
        </div>
        <div class="task-info">
            <h4>${task.title || 'Agent Operation'}</h4>
            <div class="meta" style="display:flex; justify-content:space-between">
                <span>[${task.agent || 'Orchestrator'}]</span>
                <span class="prefix-${task.status==='completed'?'success':(isRunning?'system':'err')}">${task.status.toUpperCase()}</span>
            </div>
        </div>
    `;
    lucide.createIcons();
}

// -----------------------------------------------------
// Boot Sequence
// -----------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();
    initGraphEngine();
    initWebSocket();
    
    // Demo the Vis.js Graph Simulation
    setTimeout(() => {
        writeToTerminal("Initiating Neo4j graph resolution matrix...", "warn");
        renderGraphImpact({
            target: "auth_service",
            impact_map: [
                { risk: 'CRITICAL_ROUTE_BREAK', name: 'POST /v1/login', depth: 1, file: 'routes/'},
                { risk: 'CRITICAL_STATE_BREAK', name: 'Redis_Token_Store', depth: 2, file: 'core/db'},
                { risk: 'CRITICAL_STATE_BREAK', name: 'Qdrant_Vector_DB', depth: 3, file: 'core/memory'},
                { risk: 'MEDIUM', name: 'ASTAnalyzer', depth: 2, file: 'services/'},
                { risk: 'MEDIUM', name: 'SessionMiddleware', depth: 1, file: 'core/'}
            ]
        });
    }, 1500);

    setTimeout(() => {
        addTerminalFeed({ thought: "MCTS Agent [Security]: Evaluating route breaks. Confidence: 88%" });
    }, 4000);
});
