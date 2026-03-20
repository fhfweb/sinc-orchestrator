import os

evolution_path = r"g:\Fernando\project0\ai-orchestrator\services\memory_evolution.py"
graph_intel_path = r"g:\Fernando\project0\ai-orchestrator\services\graph_intelligence.py"

# --- 1. memory_evolution.py: verified flag propagation ---
with open(evolution_path, 'r', encoding='utf-8') as f:
    ev_content = f.read()

# Update signature
ev_content = ev_content.replace(
    "succeeded: bool,\n    error: Optional[str] = None",
    "succeeded: bool,\n    error: Optional[str] = None,\n    verified: bool = False"
)

# Update cr.store_solution call
ev_content = ev_content.replace(
    "cr.store_solution,\n                description, f\"Lesson: {lesson_text}\\nSolution: {solution}\", project_id, tenant_id,\n                intent=task_type\n            )",
    "cr.store_solution,\n                description, f\"Lesson: {lesson_text}\\nSolution: {solution}\", project_id, tenant_id,\n                intent=task_type, verified=verified\n            )"
)

with open(evolution_path, 'w', encoding='utf-8') as f:
    f.write(ev_content)

# --- 2. graph_intelligence.py: Professional GDS Lifecycle ---
with open(graph_intel_path, 'r', encoding='utf-8') as f:
    gi_content = f.read()

old_gds_logic = """    def run_reputation_gds(self):
        \"\"\"
        Surgical GDS implementation replacing the O(N) iterative Cypher trap.
        Uses PageRank for cross-task influence and Degree Centrality for structural diversity.
        \"\"\"
        try:
            driver = self._get_driver()
            with driver.session() as session:
                # 1. Verification: Does GDS exist?
                gds_check = session.run(\"RETURN gds.version() AS v\").single()
                if not gds_check:
                    log.warning(\"GDS library not found. Falling back to simple metrics.\")
                    return

                # 2. Lifecycle: Clean up and project
                session.run(\"CALL gds.graph.drop('reputationGraph', false)\")
                session.run(\"\"\"
                CALL gds.graph.project(
                  'reputationGraph',
                  ['Agent', 'Task'],
                  {
                    PERFORMED: { orientation: 'NATURAL' },
                    DEPENDS_ON: { orientation: 'REVERSE' }
                  }
                )
                \"\"\")

                # 3. Execution: PageRank (Influence propagation)
                session.run(\"\"\"
                CALL gds.pageRank.write('reputationGraph', {
                   writeProperty: 'pagerank_score',
                   maxIterations: 20,
                   dampingFactor: 0.85
                })
                \"\"\")

                # 4. Execution: Degree Centrality (Task diversity/load)
                session.run(\"\"\"
                CALL gds.degree.write('reputationGraph', {
                  writeProperty: 'centrality_score'
                })
                \"\"\")

                # 5. Cleanup
                session.run(\"CALL gds.graph.drop('reputationGraph')\")

            log.info(\"gds_architectural_remediation_complete\")
        except Exception as e:
            log.error(\"run_reputation_gds_failed error=%s\", e)"""

new_gds_logic = """    def run_reputation_gds(self):
        \"\"\"
        Professional GDS Lifecycle Management.
        Uses persisted native projections to handle 1M+ nodes efficiently.
        \"\"\"
        graph_name = 'reputationGraph_v2'
        try:
            driver = self._get_driver()
            with driver.session() as session:
                # 1. Verification: Does GDS exist?
                gds_res = session.run(\"RETURN gds.version() AS v\").single()
                if not gds_res:
                    log.warning(\"GDS_NOT_FOUND fallback_to_heuristic\")
                    return

                # 2. Professional Lifecycle: Check existence before drop/project
                check_query = \"CALL gds.graph.exists($name) YIELD exists\"
                if session.run(check_query, name=graph_name).single()[\"exists\"]:
                    session.run(\"CALL gds.graph.drop($name)\", name=graph_name)

                # 3. Native Projection with Property Mapping (High Performance)
                session.run(\"\"\"
                CALL gds.graph.project(
                  $name,
                  {
                    Agent: { label: 'Agent' },
                    Task: { label: 'Task', properties: ['success', 'duration_ms'] }
                  },
                  {
                    PERFORMED: { type: 'PERFORMED', orientation: 'NATURAL' },
                    DEPENDS_ON: { type: 'DEPENDS_ON', orientation: 'REVERSE' }
                  }
                )
                \"\"\", name=graph_name)

                # 4. Execution: PageRank (Dynamic Influence Analysis)
                session.run(\"\"\"
                CALL gds.pageRank.write($name, {
                   writeProperty: 'pagerank_score',
                   maxIterations: 25,
                   dampingFactor: 0.85,
                   scaler: 'MIN_MAX'
                })
                \"\"\", name=graph_name)

                # 5. Execution: Degree Centrality (Connectivity & Load)
                session.run(\"\"\"
                CALL gds.degree.write($name, {
                  writeProperty: 'centrality_score'
                })
                \"\"\", name=graph_name)

                # 6. Memory Management: Explicitly drop to free up GDS RAM
                session.run(\"CALL gds.graph.drop($name)\", name=graph_name)

            log.info(\"gds_lifecycle_run_complete graph=%s\", graph_name)
        except Exception as e:
            log.error(\"run_reputation_gds_failed error=%s\", e)"""

gi_content = gi_content.replace(old_gds_logic, new_gds_logic)

with open(graph_intel_path, 'w', encoding='utf-8') as f:
    f.write(gi_content)

print("Applied GDS Professionalization and Memory Evolution fixes.")
