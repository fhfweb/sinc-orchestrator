import os
import json
import psycopg2
from neo4j import GraphDatabase
from dotenv import load_dotenv
from pathlib import Path

# Load env from Laravel .env
laravel_env = Path("g:/Fernando/project0/workspace/projects/sistema-gestao-psicologos-autonomos/.env")
load_dotenv(laravel_env)

def sync():
    # DB Config
    conn = psycopg2.connect(
        host="localhost", # Since we are running from host
        port=5432,
        database=os.getenv("DB_DATABASE"),
        user=os.getenv("DB_USERNAME"),
        password=os.getenv("DB_PASSWORD")
    )
    
    # Neo4j Config
    neo4j_uri = "bolt://localhost:7687"
    neo4j_user = "neo4j"
    neo4j_password = "6c887da889bce4c756657f2e2c2f712be66fbcce099cc6de"
    
    driver = GraphDatabase.driver(neo4j_uri, auth=(neo4j_user, neo4j_password))
    
    with conn.cursor() as cur:
        # Fetch Patients
        cur.execute("SELECT id, name FROM patients")
        patients = cur.fetchall()
        
        # Fetch Medical Records
        cur.execute("SELECT id, patient_id, ai_summary, ai_risk_score, ai_analysis_at FROM medical_records")
        records = cur.fetchall()
        
    with driver.session() as session:
        # Create Patient Nodes
        for p_id, name in patients:
            session.run("""
                MERGE (p:MemoryNode {id: $id})
                SET p.name = $name,
                    p.node_type = 'patient',
                    p.label = 'Patient',
                    p.summary = $summary,
                    p.project_slug = 'sistema-gestao-psicologos-autonomos'
            """, id=f"patient::{p_id}", name=name, summary=f"Paciente: {name}")
            
        # Create Record Nodes and Relationships
        for r_id, p_id, summary, risk, analysis_at in records:
            risk_val = float(risk) if risk else 0.0
            session.run("""
                MERGE (r:MemoryNode {id: $id})
                SET r.node_type = 'medical_record',
                    r.label = 'MedicalRecord',
                    r.summary = $summary,
                    r.risk_score = $risk,
                    r.analysis_at = $analysis_at,
                    r.project_slug = 'sistema-gestao-psicologos-autonomos'
                WITH r
                MATCH (p:MemoryNode {id: $p_id})
                MERGE (p)-[:HAS_RECORD]->(r)
            """, id=f"record::{r_id}", summary=summary or "Sem resumo de IA", 
                 risk=risk_val, analysis_at=str(analysis_at), p_id=f"patient::{p_id}")
            
    conn.close()
    driver.close()
    print("Clinical data synced to Neo4j successfully.")

if __name__ == "__main__":
    sync()
