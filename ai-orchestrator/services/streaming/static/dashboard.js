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
    systemMode: document.getElementById('system-mode-display'),
    terminalFeed: document.getElementById('terminal-feed-container'),
    graphContainer: document.getElementById('graph-network-container'),
    burnRate: document.getElementById('metric-burn-rate')
};

const kCols = {
    'pending': document.querySelector('#col-pending .k-cards'),
    'running': document.querySelector('#col-running .k-cards'),
    'review': document.querySelector('#col-review .k-cards'),
    'completed': document.querySelector('#col-done .k-cards'),
    'done': document.querySelector('#col-done .k-cards')
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

    // Focus Mode (Red Team Heatmap)
    APP_STATE.network.on("click", function (params) {
        if (params.nodes.length > 0) {
            const nodeId = params.nodes[0];
            
            // Zoom in aggressively
            APP_STATE.network.focus(nodeId, {
                scale: 1.8,
                animation: { duration: 1200, easingFunction: "easeInOutQuad" }
            });
            
            // Apply Red Team Visuals
            const clickedNode = APP_STATE.graphNodes.get(nodeId);
            APP_STATE.graphNodes.update({
                id: nodeId, 
                color: { background: '#ff0033', border: '#ff4d6d' }, 
                shadow: { enabled: true, color: '#ff0033', size: 35 }
            });
            writeToTerminal(`[L2 MEMORY] Focus Mode engaged on vector node: ${clickedNode.label || nodeId}`, "warn");
            
            // Dim edges temporarily
            APP_STATE.network.setOptions({ edges: { color: { color: 'rgba(255, 0, 50, 0.1)' } } });
        } else {
            // Reset View
            APP_STATE.network.fit({
                animation: { duration: 1000, easingFunction: "easeInOutQuad" }
            });
            APP_STATE.network.setOptions(options); // Restore default options
        }
    });
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
    if (!task.task_id && !task.id) return;
    const tId = task.task_id || task.id;
    const taskId = `task-${tId}`;
    let el = document.getElementById(taskId);
    
    // Status Resolution
    let colId = task.status || 'pending';
    if (colId === 'in_progress' || colId === 'running') colId = 'running';
    else if (colId === 'done' || colId === 'success' || colId === 'completed') colId = 'completed';
    else if (colId === 'review' || colId === 'hil') colId = 'review';
    else colId = 'pending';
    
    const targetCol = kCols[colId] || kCols['pending'];
    if (!targetCol) return;

    if(!el) {
        el = document.createElement('div');
        el.id = taskId;
        targetCol.prepend(el);
    } else {
        if (el.parentElement !== targetCol) {
            targetCol.prepend(el);
        }
    }
    
    // Decorate CSS Classes
    el.className = `task-item ${colId === 'running' ? 'running' : (colId === 'review' ? 'review' : (colId === 'completed' ? 'done' : ''))}`;
    const icon = colId === 'running' ? 'loader' : (colId === 'completed' ? 'check-circle' : (colId === 'review' ? 'eye' : 'clock'));
    const isSpinning = colId === 'running' ? 'rotating' : '';
    
    let html = `
        <div class="task-title">${task.title || 'MCTS Evaluation Node'}</div>
        <div class="task-agent">
            <i data-lucide="${icon}" class="${isSpinning}" style="width:14px; height:14px;"></i> 
            ${task.agent || 'SINC Orchestrator'}
        </div>
    `;

    if (colId === 'review') {
        html += `
            <div class="hil-actions">
                <button class="hil-btn approve" onclick="handleHIL('${tId}', 'approve')">APPROVE</button>
                <button class="hil-btn reject" onclick="handleHIL('${tId}', 'reject')">REJECT</button>
            </div>
        `;
    }

    el.innerHTML = html;
    
    // KanBan Maintenance (Cap to 15 items per column max)
    if(targetCol.children.length > 15) {
        targetCol.removeChild(targetCol.lastChild);
    }
    
    if (window.lucide) window.lucide.createIcons();
}

window.handleHIL = function(taskId, action) {
    if (action === 'approve') {
        writeToTerminal(`[HIL] User APPROVED task: ${taskId}. Resuming MCTS execution branch...`, "success");
        renderTaskUpdate({task_id: taskId, status: 'running', title: 'Applying architecture changes...', agent: 'ArchitectAgent'});
        setTimeout(() => renderTaskUpdate({task_id: taskId, status: 'completed', title: 'Changes merged seamlessly.', agent: 'System'}), 2500);
    } else {
        writeToTerminal(`[HIL] User REJECTED task: ${taskId}. Halting branch evaluation.`, "err");
        const el = document.getElementById(`task-${taskId}`);
        if(el) el.remove();
    }
};

// -----------------------------------------------------
// Command Palette (Cmd+K)
// -----------------------------------------------------
const cmdDialog = document.getElementById('cmd-palette');
const cmdInput = document.getElementById('cmd-input');
const cmdResults = document.getElementById('cmd-results');

document.addEventListener('keydown', (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === 'k') {
        e.preventDefault();
        if (cmdDialog.open) {
            cmdDialog.close();
        } else {
            cmdDialog.showModal();
            cmdInput.value = '';
            cmdInput.focus();
        }
    }
});

// Click outside to close
cmdDialog.addEventListener('click', (e) => {
    if (e.target === cmdDialog) cmdDialog.close();
});

let debounceTimer;
cmdInput.addEventListener('input', (e) => {
    clearTimeout(debounceTimer);
    const val = e.target.value;
    
    if(!val) {
        cmdResults.innerHTML = `<div class="cmd-item hint"><i data-lucide="info"></i> Try typing <span>/search [query]</span> or <span>/kill</span></div>`;
        lucide.createIcons();
        return;
    }

    if(val === '/kill') {
        cmdResults.innerHTML = `
            <div class="cmd-item" style="border-color: rgba(240, 50, 80, 0.5); background: rgba(240,50,80,0.1)">
                <i data-lucide="alert-triangle" style="color:var(--danger)"></i>
                <div class="cmd-result-content">
                    <div class="cmd-result-title" style="color:var(--danger)">Trigger Circuit Breaker</div>
                    <div class="cmd-result-desc">Halt all orchestration across the cluster instantly.</div>
                </div>
            </div>`;
        lucide.createIcons();
        return;
    }

    // Default to L3 Memory Search if natural language or /search
    let query = val;
    if(val.startsWith('/search ')) query = val.replace('/search ', '');
    
    cmdResults.innerHTML = `<div class="cmd-item hint"><i data-lucide="loader" class="rotating"></i> Searching SINC L3 Neural Cache...</div>`;
    lucide.createIcons();
    
    debounceTimer = setTimeout(() => {
        fetch(`/api/v5/dashboard/cognitive/memory/search?query=${encodeURIComponent(query)}&tenant_id=${APP_STATE.tenant_id}`)
            .then(res => res.json())
            .then(data => {
                cmdResults.innerHTML = '';
                if(!data.ok || (!data.cache_hit && (!data.result || data.result.chunks.length === 0))) {
                    cmdResults.innerHTML = `<div class="cmd-item hint"><i data-lucide="database"></i> No L3 memory found for this syntax.</div>`;
                    lucide.createIcons();
                    return;
                }

                // Render Cache Hit
                if(data.cache_hit) {
                    cmdResults.innerHTML += `
                        <div class="cmd-item active" style="border-color: var(--primary)">
                            <i data-lucide="brain" style="color:var(--primary)"></i>
                            <div class="cmd-result-content">
                                <div class="cmd-result-title">SINC Solution Memory <span class="rag-score">Score: ${data.cache_hit.score.toFixed(2)}</span></div>
                                <div class="cmd-result-desc">${data.cache_hit.answer}</div>
                            </div>
                        </div>
                    `;
                }

                // Render Context Chunks
                if(data.result && data.result.chunks) {
                    data.result.chunks.forEach(chunk => {
                        const snippet = chunk.text.substring(0, 150).replace(/\\n/g, ' ') + '...';
                        cmdResults.innerHTML += `
                            <div class="cmd-item">
                                <i data-lucide="file-code"></i>
                                <div class="cmd-result-content">
                                    <div class="cmd-result-title">${chunk.file}</div>
                                    <div class="cmd-result-desc">${snippet}</div>
                                </div>
                            </div>
                        `;
                    });
                }
                lucide.createIcons();
            }).catch(e => {
                cmdResults.innerHTML = `<div class="cmd-item hint" style="color:var(--danger)">Error querying Memory DB.</div>`;
            });
    }, 400);
});

// -----------------------------------------------------
// SPA Routing & Tenant Management
// -----------------------------------------------------
const viewMain = document.getElementById('view-main-dashboard');
const viewEngine = document.getElementById('view-engine-room');
const navItems = document.querySelectorAll('.sidebar .nav-item');

navItems.forEach(item => {
    item.addEventListener('click', (e) => {
        navItems.forEach(n => n.classList.remove('active'));
        item.classList.add('active');
        
        if(item.textContent.includes('Cognitive Config')) {
            if(viewMain) viewMain.classList.add('hidden');
            if(viewEngine) viewEngine.classList.remove('hidden');
        } else {
            if(viewEngine) viewEngine.classList.add('hidden');
            if(viewMain) viewMain.classList.remove('hidden');
        }
    });
});

const tenantSelect = document.getElementById('tenant-selector');
if(tenantSelect) {
    tenantSelect.addEventListener('change', (e) => {
        APP_STATE.tenant_id = e.target.value;
        writeToTerminal(`[SYSTEM] Hot-swapping to Tenant: ${e.target.value.toUpperCase()}...`, "warn");
        
        if (socket) {
            socket.close();
            writeToTerminal(`[SYSTEM] Restarting MCTS Telemetry Stream for new tenant...`, "system");
            setTimeout(initWebSocket, 500);
        }
        
        document.querySelectorAll('.k-cards').forEach(c => c.innerHTML = '');
    });
}

const btnKill = document.getElementById('btn-kill-switch');
if(btnKill) {
    btnKill.addEventListener('click', () => {
        writeToTerminal(`[SECURITY] INITIATING GLOBAL CIRCUIT BREAKER!`, "err");
        writeToTerminal(`[SECURITY] Halting ALL Agents, Planners, and Memory stores.`, "err");
        
        document.body.style.animation = 'pulse-danger 2s infinite';
        
        if (socket) socket.close();
        document.querySelectorAll('.task-item').forEach(el => el.classList.remove('running'));
        
        if (window.metricOdometers && window.metricOdometers.activeAgents) {
            window.metricOdometers.activeAgents.update(0);
        }
    });
}

// -----------------------------------------------------
// Particles.js Neural Background
// -----------------------------------------------------
function initParticles() {
    if(window.particlesJS) {
        particlesJS("particles-js", {
            "particles": {
                "number": { "value": 60, "density": { "enable": true, "value_area": 800 } },
                "color": { "value": "#00ffaa" },
                "shape": { "type": "circle" },
                "opacity": { "value": 0.4, "random": true, "anim": { "enable": true, "speed": 1, "opacity_min": 0.1, "sync": false } },
                "size": { "value": 3, "random": true, "anim": { "enable": true, "speed": 2, "size_min": 0.1, "sync": false } },
                "line_linked": { "enable": true, "distance": 150, "color": "#8a4bff", "opacity": 0.3, "width": 1 },
                "move": { "enable": true, "speed": 1.5, "direction": "none", "random": true, "straight": false, "out_mode": "out", "bounce": false }
            },
            "interactivity": {
                "detect_on": "window",
                "events": {
                    "onhover": { "enable": true, "mode": "grab" },
                    "onclick": { "enable": true, "mode": "push" },
                    "resize": true
                },
                "modes": {
                    "grab": { "distance": 220, "line_linked": { "opacity": 0.8 } },
                    "push": { "particles_nb": 4 }
                }
            },
            "retina_detect": true
        });
    }
}

// -----------------------------------------------------
// Boot Sequence
// -----------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();
    initParticles();
    initGraphEngine();
    initWebSocket();
    
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
