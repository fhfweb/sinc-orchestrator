import os
import json
import sys
import re
import subprocess
from pathlib import Path

def run_db_cmd(mode, project_root, json_path=None, dag_path=None):
    """Executes task_state_db.py as a subprocess to ensure environment parity."""
    cmd = [sys.executable, "task_state_db.py", "--mode", mode, "--project-path", str(project_root)]
    if json_path:
        cmd.extend(["--tasks-json-path", str(json_path)])
    if dag_path:
        cmd.extend(["--dag-path", str(dag_path)])
    
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Error running {mode}: {result.stderr}")
    return result.stdout

def migrate_whiteboard(project_root):
    wb_path = project_root / "ai-orchestrator" / "state" / "whiteboard.json"
    if not wb_path.exists():
        print("No whiteboard.json found.")
        return
    
    print(f"Migrating whiteboard from {wb_path}...")
    run_db_cmd("write-whiteboard", project_root, json_path=wb_path)

def migrate_incidents(project_root):
    reports_dir = project_root / "ai-orchestrator" / "reports"
    if not reports_dir.exists():
        print("No reports directory found.")
        return
    
    # We look for INCIDENT_*.md files
    incident_files = list(reports_dir.glob("INCIDENT_*.md"))
    print(f"Found {len(incident_files)} incident reports.")
    
    for f in incident_files:
        print(f"Migrating incident {f.name}...")
        try:
            content = f.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  Error reading {f.name}: {e}")
            continue
            
        # Basic parsing of the incident Markdown format defined in Common.ps1 New-IncidentReport
        category = ""
        title = ""
        details = ""
        command = ""
        output = ""
        
        cat_match = re.search(r"- Category: (.*)", content)
        if cat_match: category = cat_match.group(1).strip()
        
        title_match = re.search(r"## Title\n(.*?)(?=\n\n##|\n\r\n##|\r\n\r\n##|$)", content, re.S)
        if title_match: title = title_match.group(1).strip()
        
        details_match = re.search(r"## Details\n(.*?)(?=\n\n##|\n\r\n##|\r\n\r\n##|$)", content, re.S)
        if details_match: details = details_match.group(1).strip()
        
        cmd_match = re.search(r"## Command\n```text\n(.*?)\n```", content, re.S)
        if cmd_match: command = cmd_match.group(1).strip()
        
        out_match = re.search(r"## Output Tail\n```text\n(.*?)\n```", content, re.S)
        if out_match: output = out_match.group(1).strip()
        
        payload = {
            "category": category,
            "title": title,
            "details": details,
            "command_text": command,
            "output_text": output,
            "incident_path": str(f)
        }
        
        import uuid
        tmp_name = f"tmp_inc_{uuid.uuid4().hex[:8]}.json"
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            run_db_cmd("record-incident", project_root, json_path=tmp_path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

def migrate_lessons(project_root):
    lessons_dir = project_root / "ai-orchestrator" / "knowledge_base" / "lessons_learned"
    if not lessons_dir.exists():
        print("No lessons directory found.")
        return
    
    lesson_files = list(lessons_dir.glob("LESSON_*.md"))
    print(f"Found {len(lesson_files)} lessons.")
    
    for f in lesson_files:
        print(f"Migrating lesson {f.name}...")
        try:
            content = f.read_text(encoding="utf-8")
        except Exception as e:
            print(f"  Error reading {f.name}: {e}")
            continue
            
        # Basic parsing of the lesson Markdown format
        source = ""
        lesson_text = ""
        
        source_match = re.search(r"- Extracted From: (.*)", content)
        if source_match: source = source_match.group(1).strip()
        
        content_match = re.search(r"## Content\n(.*?)(?=\n$|\r\n$|$)", content, re.S)
        if content_match: lesson_text = content_match.group(1).strip()
        
        payload = {
            "task_id": "MIGRATED",
            "category": "migrated",
            "lesson": lesson_text,
            "source_file": source if source else str(f)
        }
        
        import uuid
        tmp_name = f"tmp_les_{uuid.uuid4().hex[:8]}.json"
        tmp_path = Path(tmp_name)
        try:
            tmp_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            run_db_cmd("record-lesson", project_root, json_path=tmp_path)
        finally:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                pass

if __name__ == "__main__":
    # Project root is two levels up from scripts/v2/
    project_root = Path(__file__).resolve().parent.parent.parent
    print(f"Starting migration for Project Root: {project_root}")
    
    migrate_whiteboard(project_root)
    migrate_incidents(project_root)
    migrate_lessons(project_root)
    
    print("\n[SUCCESS] Memory migration to database completed.")
