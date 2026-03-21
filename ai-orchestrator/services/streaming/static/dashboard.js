// Cognitive NOC Dashboard Logic
const APP_STATE = {
    ws: null,
    tenant_id: 'default',
    reconnect_attempts: 0,
    max_reconnect: 5
};

// UI Elements
const els = {
    successRate: document.getElementById('metric-success-rate'),
    autonomyScore: document.getElementById('metric-autonomy-score'),
    activeAgents: document.getElementById('metric-active-agents'),
    latency: document.getElementById('metric-latency'),
    systemMode: document.getElementById('system-mode-display'),
    blastContainer: document.getElementById('blast-radius-container'),
    taskList: document.getElementById('task-list-container'),
    activityFeed: document.getElementById('activity-feed-container')
};

function initWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/v5/dashboard/ws/telemetry?tenant_id=${APP_STATE.tenant_id}`;

    APP_STATE.ws = new WebSocket(wsUrl);

    APP_STATE.ws.onopen = () => {
        console.log("Connected to Cognitive Telemetry Stream.");
        APP_STATE.reconnect_attempts = 0;
        updateConnectionStatus(true);
    };

    APP_STATE.ws.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleTelemetryEvent(data);
        } catch (e) {
            console.error("Invalid telemetry payload", e);
        }
    };

    APP_STATE.ws.onclose = () => {
        updateConnectionStatus(false);
        if (APP_STATE.reconnect_attempts < APP_STATE.max_reconnect) {
            APP_STATE.reconnect_attempts++;
            setTimeout(initWebSocket, 2000 * APP_STATE.reconnect_attempts);
        }
    };
}

function updateConnectionStatus(isOnline) {
    const indicator = document.querySelector('.status-indicator');
    const label = indicator.nextElementSibling.querySelector('span:first-child');
    
    if (isOnline) {
        indicator.style.background = 'var(--success)';
        indicator.style.boxShadow = '0 0 10px var(--success)';
        label.textContent = "System Online";
    } else {
        indicator.style.background = 'var(--danger)';
        indicator.style.boxShadow = '0 0 10px var(--danger)';
        indicator.style.animation = 'none';
        label.textContent = "Connection Lost";
    }
}

function handleTelemetryEvent(data) {
    // Determine event type
    if (data.type === 'summary' || data.metrics) {
        updateMetrics(data);
    }
    if (data.type === 'task_transition') {
        renderTaskUpdate(data);
    }
    if (data.type === 'blast_radius' || data.impact_map) {
        renderBlastRadius(data);
    }
    if (data.type === 'agent_thought' || data.mcts) {
        addMCTSFeed(data);
    }

    // Default processing for normal REST hydration if missing WebSocket fields
    if (data.routing) updateMode(data.autonomy?.mode || 'AUTONOMOUS');
}

function updateMetrics(data) {
    if(!data.metrics) return;
    
    const m = data.metrics;
    if(m.success_rate !== undefined) els.successRate.textContent = `${(m.success_rate).toFixed(1)}%`;
    if(m.autonomy_score !== undefined) els.autonomyScore.textContent = `${(m.autonomy_score).toFixed(1)}`;
    if(m.active_agents !== undefined) els.activeAgents.textContent = m.active_agents;
    if(m.latency_p95 !== undefined) els.latency.textContent = `${Math.round(m.latency_p95)}ms`;
}

function updateMode(mode) {
    els.systemMode.textContent = mode.toUpperCase();
}

// -----------------------------------------------------
// Blast Radius Radar Visualization
// -----------------------------------------------------
function renderBlastRadius(data) {
    if(!data || !data.impact_map || data.impact_map.length === 0) return;
    
    // Clear old rings (keep target and rings)
    const oldNodes = els.blastContainer.querySelectorAll('.impact-node');
    oldNodes.forEach(n => n.remove());
    
    // Render new impact nodes
    const impacts = data.impact_map;
    
    impacts.forEach((imp, i) => {
        // limit to max 5 visuals so it doesn't clutter
        if(i > 5) return;
        
        const node = document.createElement('div');
        node.className = 'impact-node';
        
        let icon = 'file-code';
        if(imp.risk === 'CRITICAL_ROUTE_BREAK') {
            node.classList.add('route');
            icon = 'network';
        } else if (imp.risk === 'CRITICAL_STATE_BREAK') {
            node.classList.add('critical');
            icon = 'database';
        }

        node.innerHTML = `
            <i data-lucide="${icon}" style="width:14px; height:14px;"></i>
            <span>${imp.name || imp.file.split('/').pop()}</span>
        `;
        
        // Random positioning around the rings
        const angle = Math.random() * Math.PI * 2;
        const distance = 80 + (imp.depth * 30); // Base radius + depth factor
        
        const cx = els.blastContainer.clientWidth / 2;
        const cy = els.blastContainer.clientHeight / 2;
        
        const x = cx + Math.cos(angle) * distance - 50; // offset for node width
        const y = cy + Math.sin(angle) * distance - 15;
        
        node.style.left = `${x}px`;
        node.style.top = `${y}px`;
        
        // entry animation
        node.style.opacity = '0';
        node.style.transform = 'scale(0.8)';
        
        els.blastContainer.appendChild(node);
        lucide.createIcons();
        
        setTimeout(() => {
            node.style.opacity = '1';
            node.style.transform = 'scale(1)';
        }, i * 150);
    });
}

// -----------------------------------------------------
// MCTS Feed & Tasks
// -----------------------------------------------------
function renderTaskUpdate(task) {
    if(els.taskList.querySelector('.loading-state')) {
        els.taskList.innerHTML = ''; // clear loading
    }
    
    const taskId = `task-${task.task_id}`;
    let el = document.getElementById(taskId);
    
    if(!el) {
        // Create new
        el = document.createElement('div');
        el.id = taskId;
        el.className = 'task-item';
        
        // keep only top 5 visually
        if(els.taskList.children.length >= 5) {
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
                <span>[${task.agent || 'Router'}]</span>
                <span style="color:var(--primary)">${task.status.toUpperCase()}</span>
            </div>
        </div>
    `;
    lucide.createIcons();
}

function addMCTSFeed(msg) {
    const feedNodes = els.activityFeed.querySelectorAll('.feed-item');
    if(feedNodes.length >= 6) {
        feedNodes[feedNodes.length - 1].remove();
    }
    
    // Clear active from old
    feedNodes.forEach(n => n.classList.remove('active'));
    
    const el = document.createElement('div');
    el.className = 'feed-item active';
    el.innerHTML = `
        <div class="time">Just now - MCTS Path Evaluation</div>
        <p>${msg.thought || msg.message || 'Evaluating task optimal branches...'}</p>
    `;
    
    els.activityFeed.prepend(el);
}

// -----------------------------------------------------
// Initialization
// -----------------------------------------------------
document.addEventListener('DOMContentLoaded', () => {
    lucide.createIcons();
    initWebSocket();
    
    // Demo the blast radius randomly (since WS depends on active usage)
    setTimeout(() => {
        renderBlastRadius({
            impact_map: [
                { risk: 'CRITICAL_ROUTE_BREAK', name: 'POST /tasks/decompose', depth: 1, file: 'routes/'},
                { risk: 'CRITICAL_STATE_BREAK', name: 'Redis_Connection_Pool', depth: 2, file: 'core/db'},
                { risk: 'MEDIUM', name: 'ASTAnalyzer', depth: 2, file: 'services/'}
            ]
        });
    }, 2000);
});
