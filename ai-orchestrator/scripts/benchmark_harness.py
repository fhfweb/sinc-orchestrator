import json
import time
import uuid
import sys
from pathlib import Path

# Fix module resolution
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ai-orchestrator"))

from services.local_agent_runner import HybridAgentRunner

def run_benchmark(iterations: int = 5, tenant_id: str = "benchmark-tenant"):
    runner = HybridAgentRunner()
    results = []
    
    print(f"🚀 Starting SINC Phase 1 Benchmark: {iterations} cycles")
    print(f"Tenant: {tenant_id}\n")
    
    tasks = [
        {"title": "Identify bottleneck", "description": "Scan the current project for circular dependencies in the services layer."},
        {"title": "Generate ADR", "description": "Create a new Architecture Decision Record for the transition to e2b sandbox execution."},
        {"title": "Refactor Memory", "description": "Optimize the L2 semantic memory retrieval to use a 0.8 cosine similarity threshold."},
        {"title": "Audit Security", "description": "Check for plaintext API keys in the environment variable discovery logic."},
        {"title": "Validate QA", "description": "Ensure the QA matching logic handles partial completion status correctly."}
    ]
    
    total_iterations = 0
    start_time = time.time()
    
    for i in range(iterations):
        task = tasks[i % len(tasks)]
        task_id = f"BENCH-{i+1}"
        print(f"[{task_id}] Executing: {task['title']}...")
        
        # Mocking task context for local runner
        dispatch = {
            "id": task_id,
            "title": task["title"],
            "description": task["description"],
            "tenant_id": tenant_id
        }
        
        res = runner.run(task["description"], task=dispatch)
        
        print(f"      Status: {res.status}")
        print(f"      Iterations: {res.iteration_count}")
        total_iterations += res.iteration_count
        
        results.append({
            "task_id": task_id,
            "title": task["title"],
            "status": res.status,
            "iterations": res.iteration_count,
            "backend": res.backend_used
        })
    
    duration = time.time() - start_time
    avg_iterations = total_iterations / iterations
    
    print("\n" + "="*40)
    print("📊 BENCHMARK REPORT: Phase 1 Baseline")
    print("="*40)
    print(f"Total Cycles:     {iterations}")
    print(f"Total Iterations: {total_iterations}")
    print(f"Avg Iter/Task:    {avg_iterations:.2f}")
    print(f"Total Duration:   {duration:.2f}s")
    print("="*40)
    
    report_path = Path("reports/phase1_baseline.json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump({
            "summary": {
                "cycles": iterations,
                "total_iterations": total_iterations,
                "avg_iterations": avg_iterations,
                "duration_s": duration
            },
            "tasks": results
        }, f, indent=2)
    print(f"Report saved to: {report_path}")

if __name__ == "__main__":
    import sys
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    run_benchmark(count)
