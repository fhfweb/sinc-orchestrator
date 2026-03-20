import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("control-plane")

app = FastAPI(title="Orchestrator V5 Control Plane")


def discover_project_root() -> Path:
    start = Path(__file__).resolve()
    for candidate in (start.parent, *start.parents):
        if (candidate / "ai-orchestrator").exists() and (candidate / "docs").exists():
            return candidate
    return Path.cwd().resolve()


PROJECT_ROOT = discover_project_root()


def get_orchestrator_root() -> Path:
    return PROJECT_ROOT / "ai-orchestrator"


def safe_read_json(path: Path, default: Any = None) -> Any:
    if default is None:
        default = {}
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"Error reading JSON from {path}: {e}")
    return default


def safe_read_markdown_tail(path: Path, lines: int = 50) -> str:
    try:
        if path.exists():
            content = path.read_text(encoding="utf-8").splitlines()
            return "\n".join(content[-lines:])
    except Exception as e:
        logger.error(f"Error reading Markdown from {path}: {e}")
    return ""


@app.get("/api/state")
def get_state() -> JSONResponse:
    root = get_orchestrator_root()
    state_path = root / "state" / "project-state.json"
    health_path = root / "state" / "health-report.json"
    locks_path = root / "locks" / "locks.json"
    
    return JSONResponse({
        "project_state": safe_read_json(state_path),
        "health": safe_read_json(health_path),
        "locks": safe_read_json(locks_path, {"active_locks": []}),
    })


@app.get("/api/dag")
def get_dag() -> JSONResponse:
    root = get_orchestrator_root()
    dag_path = root / "tasks" / "task-dag.json"
    return JSONResponse(safe_read_json(dag_path, {"tasks": []}))


@app.get("/api/logs")
def get_logs() -> JSONResponse:
    root = get_orchestrator_root()
    history_path = root / "tasks" / "execution-history.md"
    messages_path = root / "communication" / "messages.md"
    
    return JSONResponse({
        "execution_history": safe_read_markdown_tail(history_path, 100),
        "messages": safe_read_markdown_tail(messages_path, 100),
    })


HTML_CONTENT = """
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Orchestrator V5 | Control Plane</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        dark: '#0f172a',
                        darker: '#020617',
                        card: '#1e293b',
                        accent: '#38bdf8',
                        success: '#22c55e',
                        warning: '#eab308',
                        danger: '#ef4444',
                        muted: '#64748b'
                    }
                }
            }
        }
    </script>
    <style>
        ::-webkit-scrollbar { width: 8px; height: 8px; }
        ::-webkit-scrollbar-track { background: #0f172a; }
        ::-webkit-scrollbar-thumb { background: #334155; border-radius: 4px; }
        ::-webkit-scrollbar-thumb:hover { background: #475569; }
        .log-container { font-family: 'Courier New', Courier, monospace; }
        .blob { animation: pulse 2s cubic-bezier(0.4, 0, 0.6, 1) infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: .5; } }
    </style>
</head>
<body class="bg-darker text-slate-200 h-screen flex flex-col overflow-hidden font-sans">
    
    <!-- Header -->
    <header class="bg-card border-b border-white/10 px-6 py-4 flex justify-between items-center shadow-lg z-10">
        <div class="flex items-center gap-3">
            <div class="w-3 h-3 bg-accent rounded-full blob"></div>
            <h1 class="text-xl font-bold tracking-wider text-white">ORCHESTRATOR <span class="text-accent">V5</span></h1>
            <span class="px-2 py-0.5 rounded text-xs bg-slate-800 text-slate-400 border border-slate-700 ml-2" id="project-name">Loading...</span>
        </div>
        <div class="flex items-center gap-4">
            <div class="text-sm font-medium id='system-status'">
                <span class="text-muted">Status: </span>
                <span id="health-badge" class="px-2 py-1 rounded bg-slate-800 text-slate-300">Syncing...</span>
            </div>
            <button onclick="refreshData()" class="p-2 rounded hover:bg-slate-700 transition cursor-pointer" title="Refresh">
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="text-slate-400 hover:text-white"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
            </button>
        </div>
    </header>

    <!-- Main Content -->
    <main class="flex-1 overflow-hidden p-6 flex gap-6">
        
        <!-- Left Column: DAG & Tasks -->
        <section class="w-2/3 flex flex-col gap-6 overflow-hidden">
            <!-- Stats Bar -->
            <div class="grid grid-cols-4 gap-4 flex-none">
                <div class="bg-card rounded-lg p-4 border border-white/5 flex flex-col">
                    <span class="text-muted text-xs uppercase font-bold tracking-wider">Done</span>
                    <span class="text-3xl font-light text-success mt-1" id="stat-done">0</span>
                </div>
                <div class="bg-card rounded-lg p-4 border border-white/5 flex flex-col">
                    <span class="text-muted text-xs uppercase font-bold tracking-wider">In Progress</span>
                    <span class="text-3xl font-light text-accent mt-1" id="stat-progress">0</span>
                </div>
                <div class="bg-card rounded-lg p-4 border border-white/5 flex flex-col">
                    <span class="text-muted text-xs uppercase font-bold tracking-wider">Pending</span>
                    <span class="text-3xl font-light text-slate-300 mt-1" id="stat-pending">0</span>
                </div>
                <div class="bg-card rounded-lg p-4 border border-white/5 flex flex-col">
                    <span class="text-muted text-xs uppercase font-bold tracking-wider">Blocked / Revision</span>
                    <span class="text-3xl font-light text-danger mt-1" id="stat-blocked">0</span>
                </div>
            </div>

            <!-- Task Board -->
            <div class="flex-1 bg-card rounded-lg border border-white/5 flex flex-col overflow-hidden">
                <div class="px-5 py-4 border-b border-white/5 flex justify-between items-center bg-slate-800/50">
                    <h2 class="text-sm font-bold uppercase tracking-widest text-slate-300">Active Task Matrix</h2>
                    <span class="text-xs text-muted" id="last-updated">Never</span>
                </div>
                <div class="flex-1 overflow-auto p-5">
                    <div id="tasks-container" class="grid grid-cols-1 xl:grid-cols-2 gap-4">
                        <!-- Tasks injected here -->
                    </div>
                </div>
            </div>
        </section>

        <!-- Right Column: State & Logs -->
        <section class="w-1/3 flex flex-col gap-6 overflow-hidden">
            <!-- Active Agents / Locks -->
            <div class="h-1/3 bg-card rounded-lg border border-white/5 flex flex-col overflow-hidden">
                <div class="px-4 py-3 border-b border-white/5 bg-slate-800/50">
                    <h2 class="text-xs font-bold uppercase tracking-widest text-slate-300">Active Agent Locks</h2>
                </div>
                <div class="flex-1 overflow-auto p-4 gap-2 flex flex-col" id="locks-container">
                    <!-- Locks injected here -->
                </div>
            </div>

            <!-- Execution Logs -->
            <div class="flex-1 bg-card rounded-lg border border-white/5 flex flex-col overflow-hidden">
                <div class="px-4 py-3 border-b border-white/5 bg-slate-800/50 flex justify-between items-center">
                    <h2 class="text-xs font-bold uppercase tracking-widest text-slate-300">System Telemetry</h2>
                    <div class="flex gap-2">
                        <button onclick="switchLog('history')" id="btn-hist" class="text-xs px-2 py-1 rounded bg-slate-700 text-white hover:bg-slate-600 transition">History</button>
                        <button onclick="switchLog('messages')" id="btn-msg" class="text-xs px-2 py-1 rounded bg-slate-800 text-slate-400 border border-slate-700 hover:text-white transition">Agent Messages</button>
                    </div>
                </div>
                <div class="flex-1 overflow-auto p-4 bg-[#0a0f1c] log-container text-xs text-slate-400 whitespace-pre-wrap font-mono relative">
                    <div id="log-content">Loading stream...</div>
                </div>
            </div>
        </section>

    </main>

    <script>
        let currentLogView = 'history';
        let latestLogs = { history: '', messages: '' };

        function switchLog(view) {
            currentLogView = view;
            document.getElementById('btn-hist').className = view === 'history' ? 'text-xs px-2 py-1 rounded bg-slate-700 text-white transition' : 'text-xs px-2 py-1 rounded bg-slate-800 text-slate-400 border border-slate-700 hover:text-white transition';
            document.getElementById('btn-msg').className = view === 'messages' ? 'text-xs px-2 py-1 rounded bg-slate-700 text-white transition' : 'text-xs px-2 py-1 rounded bg-slate-800 text-slate-400 border border-slate-700 hover:text-white transition';
            renderLogs();
        }

        function renderLogs() {
            const content = currentLogView === 'history' ? latestLogs.history : latestLogs.messages;
            const container = document.getElementById('log-content');
            container.innerHTML = content ? escapeHtml(content) : '<span class="text-slate-600">No recent events.</span>';
            container.parentElement.scrollTop = container.parentElement.scrollHeight;
        }

        function escapeHtml(unsafe) {
            return unsafe
                 .replace(/&/g, "&amp;")
                 .replace(/</g, "&lt;")
                 .replace(/>/g, "&gt;")
                 .replace(/"/g, "&quot;")
                 .replace(/'/g, "&#039;");
        }

        function getStatusColor(status) {
            const s = (status || '').toLowerCase();
            if (s === 'done') return 'border-success text-success bg-success/10';
            if (s === 'in-progress') return 'border-accent text-accent bg-accent/10';
            if (s.includes('blocked') || s.includes('revision') || s.includes('error')) return 'border-danger text-danger bg-danger/10';
            return 'border-slate-600 text-slate-400 bg-slate-800/50';
        }

        function formatTime(isoStr) {
            if (!isoStr) return 'N/A';
            const d = new Date(isoStr);
            return d.toLocaleTimeString([], {hour: '2-digit', minute:'2-digit', second:'2-digit'});
        }

        async function fetchState() {
            try {
                const [stateRes, dagRes, logsRes] = await Promise.all([
                    fetch('/api/state'), fetch('/api/dag'), fetch('/api/logs')
                ]);
                const state = await stateRes.json();
                const dag = await dagRes.json();
                const logs = await logsRes.json();

                // Update Header
                const health = state.health?.health_status || 'unknown';
                const hBadge = document.getElementById('health-badge');
                hBadge.textContent = health.toUpperCase();
                hBadge.className = `px-2 py-1 rounded text-xs font-bold ${
                    health === 'healthy' ? 'bg-success/20 text-success border border-success/30' : 
                    health === 'unknown' ? 'bg-slate-800 text-slate-400' : 'bg-danger/20 text-danger border border-danger/30'
                }`;

                // Update Logs
                latestLogs.history = logs.execution_history;
                latestLogs.messages = logs.messages;
                renderLogs();

                // Update Locks
                const locksContainer = document.getElementById('locks-container');
                const activeLocks = state.locks?.active_locks || [];
                if (activeLocks.length === 0) {
                    locksContainer.innerHTML = '<div class="text-sm text-slate-500 italic p-2 text-center mt-4">System is resting. No active locks.</div>';
                } else {
                    locksContainer.innerHTML = activeLocks.map(l => `
                        <div class="bg-dark rounded border border-white/5 p-3 flex justify-between items-center group hover:border-accent/30 transition">
                            <div class="flex items-center gap-3">
                                <div class="w-2 h-2 rounded-full bg-warning blob"></div>
                                <div class="flex flex-col">
                                    <span class="text-sm font-bold text-slate-200">${l.agent_name || 'Unknown'}</span>
                                    <span class="text-xs text-muted font-mono">${l.task_id}</span>
                                </div>
                            </div>
                            <span class="text-xs text-slate-500 bg-slate-800 px-2 py-1 rounded">${l.files?.length || 0} files</span>
                        </div>
                    `).join('');
                }

                // Update Stats & DAG
                const tasks = dag.tasks || [];
                let done = 0, prog = 0, pend = 0, block = 0;
                
                // Sort tasks: in-progress first, then blocked, then pending, then done (last 10 done)
                const sortedTasks = tasks.sort((a, b) => {
                    const order = { 'in-progress': 0, 'needs-revision': 1, 'blocked-runtime': 1, 'pending': 2, 'done': 3 };
                    const sa = order[(a.status || '').toLowerCase()] ?? 2;
                    const sb = order[(b.status || '').toLowerCase()] ?? 2;
                    return sa - sb;
                });

                const tasksContainer = document.getElementById('tasks-container');
                let html = '';

                tasks.forEach(t => {
                    const s = (t.status || '').toLowerCase();
                    if (s === 'done') done++;
                    else if (s === 'in-progress') prog++;
                    else if (s.includes('blocked') || s.includes('revision')) block++;
                    else pend++;
                });

                document.getElementById('stat-done').textContent = done;
                document.getElementById('stat-progress').textContent = prog;
                document.getElementById('stat-pending').textContent = pend;
                document.getElementById('stat-blocked').textContent = block;

                // Render top 20 active/recent tasks
                const renderTasks = sortedTasks.slice(0, 30);
                renderTasks.forEach(t => {
                    const colorClasses = getStatusColor(t.status);
                    const agent = t.assigned_agent || t.preferred_agent || 'Any';
                    const s = (t.status || '').toLowerCase();
                    
                    // Show progress bar if in progress (fake animation for effect)
                    const progressHtml = s === 'in-progress' ? 
                        `<div class="w-full bg-slate-800 h-1.5 mt-3 rounded overflow-hidden">
                            <div class="bg-accent h-full w-full rounded animate-[progress_2s_ease-in-out_infinite]" style="transform-origin: left;"></div>
                         </div>` : '';

                    const errorHtml = (s.includes('blocked') || s.includes('revision')) && t.last_error ? 
                        `<div class="bg-danger/10 border border-danger/20 text-danger text-xs p-2 mt-2 rounded font-mono truncate" title="${escapeHtml(t.last_error)}">
                            ${escapeHtml(t.last_error)}
                         </div>` : '';

                    html += `
                        <div class="bg-dark rounded-lg border border-white/5 p-4 flex flex-col shadow-sm hover:border-slate-600 transition">
                            <div class="flex justify-between items-start mb-2">
                                <h3 class="font-bold text-slate-200 text-sm w-3/4 leading-tight">${escapeHtml(t.title || t.id)}</h3>
                                <span class="px-2 py-0.5 rounded-full text-[10px] font-bold uppercase border ${colorClasses}">
                                    ${t.status || 'UNKNOWN'}
                                </span>
                            </div>
                            <div class="flex items-center gap-2 mt-auto pt-2 text-xs text-muted">
                                <span class="flex items-center gap-1"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg> ${escapeHtml(agent)}</span>
                                <span class="mx-1">•</span>
                                <span class="font-mono">${t.id}</span>
                            </div>
                            ${errorHtml}
                            ${progressHtml}
                        </div>
                    `;
                });
                tasksContainer.innerHTML = html;

                document.getElementById('last-updated').textContent = new Date().toLocaleTimeString();

            } catch (e) {
                console.error("Error fetching state:", e);
                document.getElementById('health-badge').textContent = 'OFFLINE';
                document.getElementById('health-badge').className = 'px-2 py-1 rounded text-xs font-bold bg-danger/20 text-danger border border-danger/30';
            }
        }

        async function refreshData() {
            await fetchState();
        }

        // Add custom keyframe for progress animation via JS to tailwind config
        const style = document.createElement('style');
        style.innerHTML = `
            @keyframes progress {
                0% { transform: scaleX(0); }
                50% { transform: scaleX(0.5); }
                100% { transform: scaleX(1); }
            }
        `;
        document.head.appendChild(style);

        // Fetch immediately, then every 3 seconds
        refreshData();
        setInterval(refreshData, 3000);
    </script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def serve_ui():
    return HTML_CONTENT


def main():
    parser = argparse.ArgumentParser(description="Orchestrator V5 Control Plane")
    parser.add_argument("--project-path", required=True, help="Absolute path to the project root")
    parser.add_argument("--port", type=int, default=8080, help="Port to run the server on")
    args = parser.parse_args()

    global PROJECT_ROOT
    PROJECT_ROOT = Path(args.project_path).resolve()
    
    logger.info(f"Starting Control Plane on port {args.port} for project: {PROJECT_ROOT}")

    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
