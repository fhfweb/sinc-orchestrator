import os
import re

ref_path = "g:/Fernando/project0/docs/agents/dashboard_referencia.html"
target_path = "g:/Fernando/project0/docs/agents/dashboard.html"

with open(ref_path, "r", encoding="utf-8") as f:
    html = f.read()

# 1. Neutralize mock intervals
html = re.sub(r'setInterval\(tickGauges,\s*2400\);', '// setInterval(tickGauges, 2400);', html)
html = re.sub(r'setInterval\(addFeed,\s*3600\);', '// setInterval(addFeed, 3600);', html)
html = re.sub(r'setInterval\(addLog,\s*2000\);', '// setInterval(addLog, 2000);', html)
html = re.sub(r'setInterval\(\(\)\s*=>\s*\{[^}]*t\.prog[^}]*},\s*450\);', '// mock pipeline tracking disabled', html, flags=re.DOTALL)
html = re.sub(r'setInterval\(\(\)\s*=>\s*\{[^}]*lessonBase\+\+[^}]*},\s*12000\);', '// mock lesson tracking disabled', html, flags=re.DOTALL)

# 2. Prepare the real JS integration
real_js = """
// ════════════════════════════════════════════════════════════
// REAL ORCHESTRATOR V2 API INTEGRATION
// ════════════════════════════════════════════════════════════

const API_BASE = (localStorage.getItem('orchestratorApiBase') || 'http://127.0.0.1:8765').replace(/\\/+$/, '');

// 1. Fetch Dashboard State
async function fetchState() {
    try {
        let res;
        try {
            res = await fetch(API_BASE + '/dashboard/state');
        } catch {
            res = await fetch('dashboard.json?_=' + Date.now()); // Fallback
        }
        if (!res.ok) return;
        const payload = await res.json();
        const d = payload.dashboard || {};
        const sum = d.summary || {};

        // Update KPIs
        const kn0 = document.getElementById('kn0');
        if (kn0) kn0.innerHTML = `${sum.projects || 1}<span class="ksub"> tenants</span>`;
        const kn1 = document.getElementById('kn1');
        if (kn1) kn1.innerHTML = `${(sum.in_progress || 0) + (sum.pending || 0)}<span class="ksub"> tasks</span>`;
        const kn3 = document.getElementById('kn3');
        if (kn3) kn3.innerHTML = `${sum.blocked || 0}<span class="ksub"> blocked</span>`;
        
        // Update Gauges if present (GPU, CPU... using SLA metrics)
        const sm = d.slo?.metrics || {};
        if (sm.cycle_p95_ms) {
             const gv = document.getElementById('gv0'); 
             if (gv) gv.textContent = Math.min(100, Math.round(sm.cycle_p95_ms / 10)) + '%';
        }
    } catch(e) {
        console.warn('State fetch err:', e);
    }
}
setInterval(fetchState, 3000);
setTimeout(fetchState, 500);

// 2. Fetch Tasks for Pipeline
async function fetchTasks() {
    try {
        const res = await fetch(API_BASE + '/tasks?limit=25');
        if (!res.ok) return;
        const data = await res.json();
        const tList = data.tasks || [];
        
        const ql = document.getElementById('queue-list');
        if (!ql) return;
        
        ql.innerHTML = tList.map((t, i) => {
            let st = 'sched'; let bc = 'var(--t3)'; let p = 0;
            if (t.status === 'in-progress' || t.status === 'active') { st = 'run'; bc = 'var(--gr)'; p = 60; }
            else if (t.status === 'pending') { st = 'proc'; bc = 'var(--bl)'; p = 10; }
            else if (t.status.includes('block')) { st = 'pause'; bc = 'var(--am)'; p = 30; }
            else if (t.status === 'done' || t.status === 'completed') { st = 'done'; bc = 'var(--gr)'; p = 100; }
            
            const n = t.title || t.id || 'Task';
            const sub = t.assigned_agent || t.execution_mode || 'System';
            
            return `<div class="qi is-${st}">
              <div class="qh">⠿</div><div class="qr">${String(i+1).padStart(2,'0')}</div>
              <div class="qi-info"><div class="qi-name">${n}</div><div class="qi-sub">${sub}</div></div>
              <div class="qpw"><div class="qpt"><div class="qpf" style="width:${p}%;background:${bc}"></div></div></div>
              <div class="qst" style="color:${bc}">${st}</div>
            </div>`;
        }).join('');
    } catch(e) {
        console.warn('Task fetch err:', e);
    }
}
setInterval(fetchTasks, 3000);
setTimeout(fetchTasks, 700);

// 3. SSE Live Events Stream
function setupEvents() {
    const logConsole = document.getElementById('logConsole');
    const feedList = document.getElementById('feed-list');
    if (!window.EventSource || !logConsole) return;
    
    let evtSource;
    try { evtSource = new EventSource(API_BASE + '/events'); } catch(e) { return; }
    
    evtSource.onmessage = (ev) => {
        try {
            const p = JSON.parse(ev.data);
            
            // 3a. Add to log console
            const line = document.createElement('div');
            line.className = 'log-line';
            const lvl = p.level ? p.level.toUpperCase() : 'INFO';
            const cls = lvl === 'ERROR' ? 'log-lvl-error' : (lvl === 'WARN' ? 'log-lvl-warn' : 'log-lvl-info');
            const ts = new Date(p.timestamp || Date.now()).toLocaleTimeString('pt-BR');
            line.innerHTML = `<span class="log-ts">${ts}</span><span class="${cls}">[${lvl}]</span><span class="log-msg">${p.message || p.event_type}</span>`;
            logConsole.insertBefore(line, logConsole.firstChild);
            while (logConsole.children.length > 25) logConsole.removeChild(logConsole.lastChild);
            
            // 3b. Add to feed if it's a major event
            if (['task_completed', 'task_failed', 'agent_assigned'].includes(p.event_type) && feedList) {
                const el = document.createElement('div');
                el.className = 'fi entering';
                const color = p.event_type === 'task_failed' ? 'var(--rd)' : 'var(--bl)';
                el.innerHTML = `<div class="fdot" style="background:${color};box-shadow:0 0 6px ${color}"></div>
                <div class="fbody"><div class="ftitle">${p.event_type}</div><div class="fmeta">${ts} · ${p.message}</div></div>
                <div class="ftag" style="color:${color}">${lvl}</div>`;
                feedList.insertBefore(el, feedList.firstChild);
                while (feedList.children.length > 8) feedList.removeChild(feedList.lastChild);
            }
            
        } catch(e) {}
    };
}
setTimeout(setupEvents, 1000);
"""

# Append scripts
html = html.replace("</body>", "<script>\n" + real_js + "\n</script>\n</body>")

with open(target_path, "w", encoding="utf-8") as f:
    f.write(html)
print("Successfully generated real dashboard code.")
