/**
 * SINC AI Orchestrator - Professional Dashboard Logic
 * Handles real-time telemetry, component registry health, and cognitive config.
 */

const API_BASE = '/api/v5/dashboard';
let pollInterval = null;
let socket = null;

// Initialization
document.addEventListener('DOMContentLoaded', () => {
    initDashboard();
    setupEventListeners();
});

async function initDashboard() {
    // Initial load
    updateSummary();
    updateFeed();
    
    // Attempt WebSocket connection
    connectWebSocket();
}

function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/api/v5/dashboard/ws/telemetry?tenant_id=default`;
    
    console.log(`Connecting to WebSocket: ${wsUrl}`);
    socket = new WebSocket(wsUrl);
    
    socket.onopen = () => {
        console.log('Telemetry WebSocket connected.');
        if (pollInterval) {
            clearInterval(pollInterval);
            pollInterval = null;
        }
    };
    
    socket.onmessage = (event) => {
        try {
            const data = JSON.parse(event.data);
            handleTelemetryUpdate(data);
        } catch (e) {
            console.error('WS Message Error:', e);
        }
    };
    
    socket.onclose = () => {
        console.warn('Telemetry WebSocket disconnected. Falling back to polling.');
        startPolling();
        setTimeout(connectWebSocket, 10000); // Retry after 10s
    };
    
    socket.onerror = (err) => {
        console.error('WebSocket Error:', err);
    };
}

function startPolling() {
    if (!pollInterval) {
        pollInterval = setInterval(() => {
            updateSummary();
            updateFeed();
        }, 5000);
    }
}

function handleTelemetryUpdate(data) {
    if (data.type === 'summary') {
        renderMetrics(data.metrics);
        renderRegistryHealth(data.registry_health);
        renderTasks(data.pipeline);
        renderReputation(data.reputation);
        
        const modeDisplay = document.getElementById('system-mode-display');
        if (modeDisplay) {
            modeDisplay.textContent = `${data.autonomy?.mode || 'NORMAL'} MODE`.toUpperCase();
            modeDisplay.className = `mode-badge ${data.autonomy?.mode || 'normal'}`;
        }
    }
}

function setupEventListeners() {
    // Navigation handling
    document.querySelectorAll('.nav-item').forEach(item => {
        item.addEventListener('click', () => {
            document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
            item.classList.add('active');
            const view = item.getAttribute('data-view');
            // Navigate or filter view here
        });
    });

    // Close modal
    document.getElementById('close-modal').addEventListener('click', () => {
        document.getElementById('modal-container').classList.add('hidden');
    });
}

// Data Fetching & UI Updates
async function updateSummary() {
    try {
        const response = await fetch(`${API_BASE}/summary`);
        if (!response.ok) throw new Error('Failed to fetch summary');
        const data = await response.json();

        renderMetrics(data.metrics);
        renderRegistryHealth(data.registry_health);
        renderTasks(data.pipeline);
        renderReputation(data.reputation);
        
        // Update mode display
        const modeDisplay = document.getElementById('system-mode-display');
        modeDisplay.textContent = `${data.autonomy?.mode || 'NORMAL'} MODE`.toUpperCase();
        modeDisplay.className = `mode-badge ${data.autonomy?.mode || 'normal'}`;

    } catch (err) {
        console.error('Dashboard Sync Error:', err);
    }
}

function renderMetrics(metrics) {
    if (!metrics) return;
    document.getElementById('metric-success-rate').textContent = `${(metrics.success_rate * 100).toFixed(1)}%`;
    document.getElementById('metric-autonomy-score').textContent = metrics.autonomy_score.toFixed(2);
    document.getElementById('metric-active-agents').textContent = metrics.active_agents;
    document.getElementById('metric-latency').textContent = metrics.latency_p95;
}

function renderRegistryHealth(registry) {
    const container = document.getElementById('registry-health-container');
    if (!container || !registry) return;

    container.innerHTML = '';
    
    // Components we care about
    const components = [
        { id: 'admission', label: 'Admission' },
        { id: 'planner', label: 'Planner' },
        { id: 'worker', label: 'Worker' }
    ];

    components.forEach(comp => {
        const statusData = registry[comp.id] || { status: 'unknown', details: 'No data' };
        const statusClass = statusData.status === 'up' ? 'up' : (statusData.status === 'warn' ? 'warn' : 'err');
        
        const item = document.createElement('div');
        item.className = 'registry-item';
        item.innerHTML = `
            <div class="reg-status-icon ${statusClass}"></div>
            <div class="reg-name">${comp.label}</div>
            <div class="reg-detail">${statusData.status.toUpperCase()}</div>
        `;
        container.appendChild(item);
    });
}

function renderTasks(tasks) {
    const container = document.getElementById('task-list-container');
    if (!container || !tasks) return;

    if (tasks.length === 0) {
        container.innerHTML = '<div class="empty-state">No active tasks in pipeline</div>';
        return;
    }

    container.innerHTML = tasks.map(task => `
        <div class="task-item" onclick="openDebugger('${task.id}')">
            <div class="task-progress-circle" style="border: 2px solid ${task.c}">
                ${task.prog}%
            </div>
            <div class="task-info">
                <h4>${task.name}</h4>
                <div class="meta">${task.sub} • ${task.status.toUpperCase()}</div>
            </div>
            <div class="task-action">
                <i data-lucide="chevron-right"></i>
            </div>
        </div>
    `).join('');
    
    if (window.lucide) lucide.createIcons();
}

function renderReputation(reputation) {
    const container = document.getElementById('rep-list-container');
    if (!container || !reputation) return;

    container.innerHTML = reputation.map(rep => `
        <div class="rep-item" style="display: flex; justify-content: space-between; margin-bottom: 12px; font-size: 13px;">
            <span>${rep.name}</span>
            <span style="font-weight: 700; color: ${rep.color}">${rep.badge} (${rep.score}%)</span>
        </div>
    `).join('');
}

async function updateFeed() {
    try {
        const response = await fetch(`${API_BASE}/feed?limit=10`);
        const data = await response.json();
        const container = document.getElementById('activity-feed-container');
        
        container.innerHTML = data.items.map(item => `
            <div class="feed-item" style="border-left: 2px solid ${item.color}; padding-left: 12px; margin-bottom: 16px;">
                <div style="font-size: 11px; color: var(--text-dim)">${item.tag} • ${item.meta}</div>
                <div style="font-size: 13px; margin-top: 2px;">${item.title}</div>
            </div>
        `).join('');
    } catch (err) {
        console.error('Feed Sync Error:', err);
    }
}

async function openDebugger(taskId) {
    const modal = document.getElementById('modal-container');
    const content = document.getElementById('modal-content');
    
    content.innerHTML = '<h2>Loading Debugger...</h2>';
    modal.classList.remove('hidden');

    try {
        const response = await fetch(`${API_BASE}/task-debugger/${taskId}`);
        const data = await response.json();
        
        content.innerHTML = `
            <h2 style="font-family: Outfit; margin-bottom: 20px;">Task Debugger: ${data.metadata.title}</h2>
            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
                <div>
                    <h3>Context</h3>
                    <p style="font-size: 14px; opacity: 0.8; margin-top: 10px;">${data.context.reason || 'No specific reason logged.'}</p>
                </div>
                <div>
                    <h3>Status</h3>
                    <div class="mode-badge up" style="display: inline-block;">${data.metadata.status.toUpperCase()}</div>
                    <p style="margin-top: 10px; font-size: 12px; color: var(--text-dim)">Agent: ${data.metadata.assigned_agent}</p>
                </div>
            </div>
            <div style="margin-top: 30px;">
                <h3>Timeline</h3>
                <div style="max-height: 200px; overflow-y: auto; margin-top: 10px; background: rgba(0,0,0,0.2); padding: 10px; border-radius: 8px;">
                    ${data.timeline.map(e => `<div style="font-size: 12px; margin-bottom: 4px;">[${e.timestamp.split('T')[1].split('.')[0]}] ${e.event}: ${e.detail}</div>`).join('')}
                </div>
            </div>
        `;
    } catch (err) {
        content.innerHTML = `<h2>Error</h2><p>${err.message}</p>`;
    }
}
