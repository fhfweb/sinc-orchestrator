"""
SINC Orchestrator Lesson Converter v2
Converts existing knowledge_base/lessons_learned/*.md files to .json format
and captures new lessons automatically from repair task completions.

Usage:
    python lesson_converter.py --convert-all    # convert .md → .json
    python lesson_converter.py --capture TASK_ID [completion_json]
    python lesson_converter.py --capture-all    # scan completions/ for uncaptured lessons
"""

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

BASE            = Path(__file__).parent.parent.parent
LESSONS_DIR     = BASE / "knowledge_base" / "lessons_learned"
COMPLETIONS_DIR = BASE / "tasks" / "completions"
CAPTURED_INDEX  = BASE / "knowledge_base" / "captured_lessons.json"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _load_captured_index() -> set[str]:
    if CAPTURED_INDEX.exists():
        try:
            return set(json.loads(CAPTURED_INDEX.read_text(encoding="utf-8")).get("captured", []))
        except Exception:
            pass
    return set()


def _save_captured_index(captured: set[str]):
    CAPTURED_INDEX.write_text(
        json.dumps({"captured": sorted(captured), "updated_at": _now_iso()}, indent=2),
        encoding="utf-8"
    )


def _parse_md_lesson(path: Path) -> dict:
    """Parse a markdown lesson file into structured JSON."""
    content = path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines()

    lesson = {
        "id":           path.stem,
        "schema_version": "v2-lesson",
        "source_file":  str(path),
        "converted_at": _now_iso(),
        "title":        "",
        "task_id":      "",
        "agent":        "",
        "generated_at": "",
        "domain":       "",
        "tags":         [],
        "error_signature": {"category": "unknown", "title": "unknown", "details": "unknown"},
        "fix_pattern":  "",
        "validation_command": "",
        "reuse_guidance": [],
        "summary":      "",
    }

    # Extract title from H1
    for line in lines:
        m = re.match(r'^#\s+Lesson Learned:\s+(.+)', line)
        if m:
            lesson["title"] = f"Lesson: {m.group(1)}"
            lesson["task_id"] = m.group(1).strip()
            break

    # Extract metadata from bullet list at top
    for line in lines:
        m = re.match(r'^-\s+(\w[\w\s]+):\s+(.+)', line)
        if m:
            key, val = m.group(1).strip().lower().replace(" ", "_"), m.group(2).strip()
            if key == "agent":
                lesson["agent"] = val
            elif key == "generated_at":
                lesson["generated_at"] = val
            elif key == "task_id":
                lesson["task_id"] = val

    # Extract sections
    current_section = None
    section_content = []
    sections = {}

    for line in lines:
        h_match = re.match(r'^##\s+(.+)', line)
        if h_match:
            if current_section and section_content:
                sections[current_section] = "\n".join(section_content).strip()
            current_section = h_match.group(1).strip()
            section_content = []
        elif current_section:
            section_content.append(line)

    if current_section and section_content:
        sections[current_section] = "\n".join(section_content).strip()

    # Map sections to structured fields
    error_section = sections.get("Error Signature", "")
    for line in error_section.splitlines():
        m = re.match(r'^-\s+(\w+):\s+(.+)', line)
        if m:
            k, v = m.group(1).lower(), m.group(2).strip()
            if k in lesson["error_signature"]:
                lesson["error_signature"][k] = v

    lesson["fix_pattern"]        = sections.get("Fix Pattern", "")
    lesson["validation_command"] = sections.get("Validation Command", "").strip("`").strip()
    lesson["summary"]            = lesson["fix_pattern"][:300] if lesson["fix_pattern"] else ""

    reuse_text = sections.get("Reuse Guidance", "")
    lesson["reuse_guidance"] = [
        line.lstrip("-• ").strip()
        for line in reuse_text.splitlines()
        if line.strip() and line.strip() not in ("-", "•")
    ]

    # Infer domain and tags from task_id
    task_id = lesson["task_id"].upper()
    tags = []
    if "AUTH" in task_id or "RBAC" in task_id or "SECURITY" in task_id:
        lesson["domain"] = "security"
        tags.append("security")
    elif "FINANCE" in task_id or "BILLING" in task_id:
        lesson["domain"] = "finance"
        tags.append("finance")
    elif "CRM" in task_id:
        lesson["domain"] = "crm"
        tags.append("crm")
    elif "REPAIR" in task_id:
        lesson["domain"] = "repair"
        tags.append("repair")
    else:
        lesson["domain"] = "general"

    if "TENANT" in task_id:
        tags.append("multi-tenant")
    if "QUERY" in lesson["fix_pattern"].upper() or "SQL" in lesson["fix_pattern"].upper():
        tags.append("raw-sql")
    lesson["tags"] = tags

    return lesson


def convert_all_md_lessons(dry_run: bool = False) -> int:
    """Convert all .md lessons to .json format."""
    md_files = list(LESSONS_DIR.glob("*.md"))
    converted = 0

    print(f"Converting {len(md_files)} .md lessons to .json...")
    for md_file in md_files:
        json_file = md_file.with_suffix(".json")
        if json_file.exists():
            print(f"  SKIP {md_file.name} (json already exists)")
            continue

        try:
            lesson = _parse_md_lesson(md_file)
            if not dry_run:
                with open(json_file, "w", encoding="utf-8", newline="\n") as f:
                    f.write(json.dumps(lesson, indent=2, ensure_ascii=True))
            print(f"  CONVERTED {md_file.name} → domain={lesson['domain']}, tags={lesson['tags']}")
            converted += 1
        except Exception as e:
            print(f"  ERROR {md_file.name}: {e}")

    print(f"Done. {converted} files converted.")
    return converted


def capture_lesson_from_completion(task_id: str, completion: dict | None = None) -> bool:
    """
    Extract a lesson from a repair task completion and write to knowledge_base.
    """
    if not task_id.startswith("REPAIR-"):
        print(f"  SKIP {task_id}: only REPAIR-* tasks generate lessons")
        return False

    captured = _load_captured_index()
    if task_id in captured:
        print(f"  SKIP {task_id}: already captured")
        return False

    # Find completion file if not provided
    if completion is None:
        comp_files = sorted(COMPLETIONS_DIR.glob(f"{task_id}-*.json"), reverse=True)
        if not comp_files:
            print(f"  NO COMPLETION found for {task_id}")
            return False
        completion = json.loads(comp_files[0].read_text(encoding="utf-8"))

    lesson_file = LESSONS_DIR / f"LESSON_{task_id}.json"

    lesson = {
        "id":             f"LESSON_{task_id}",
        "schema_version": "v2-lesson",
        "task_id":        task_id,
        "agent":          completion.get("agent_name", "unknown"),
        "generated_at":   completion.get("validated_at", _now_iso()),
        "captured_at":    _now_iso(),
        "title":          f"Lesson from {task_id}: {completion.get('summary', '')[:80]}",
        "domain":         _infer_domain(task_id, completion),
        "tags":           _infer_tags(task_id, completion),
        "status":         completion.get("status", "unknown"),
        "summary":        completion.get("summary", ""),
        "files_modified": completion.get("files_modified", []),
        "policy_violations": completion.get("policy_violations", []),
        "fix_pattern":    completion.get("summary", ""),
        "reuse_guidance": _extract_reuse_guidance(completion),
        "error_signature": {
            "category": _infer_error_category(completion),
            "title":    task_id,
            "details":  completion.get("summary", "")[:200],
        },
        "next_tasks_suggested": completion.get("next_suggested_tasks", []),
    }

    lesson_file.write_text(json.dumps(lesson, indent=2, ensure_ascii=False), encoding="utf-8")
    captured.add(task_id)
    _save_captured_index(captured)
    print(f"  CAPTURED lesson for {task_id} → {lesson_file.name}")
    return True


def _infer_domain(task_id: str, completion: dict) -> str:
    combined = (task_id + " " + completion.get("summary", "")).upper()
    for domain, keywords in [
        ("security", ["AUTH", "RBAC", "SECURITY", "TENANT", "BYPASS"]),
        ("finance",  ["FINANCE", "BILLING", "INVOICE", "PAYMENT", "COMMISSION"]),
        ("crm",      ["CRM", "PIPELINE", "LEAD", "CAMPAIGN"]),
        ("infra",    ["DOCKER", "DEPLOY", "INFRA", "OPS"]),
        ("database", ["SQL", "QUERY", "MIGRATION", "DB", "PDO"]),
    ]:
        if any(kw in combined for kw in keywords):
            return domain
    return "repair"


def _infer_tags(task_id: str, completion: dict) -> list[str]:
    tags = ["repair"]
    combined = (task_id + " " + completion.get("summary", "") +
                " ".join(completion.get("policy_violations", []))).upper()
    keyword_tags = {
        "RAW-SQL": ["PDO", "RAW SQL", "NORAWQUERIES"],
        "SECURITY": ["SECURITY", "AUTH", "PERMISSION"],
        "MULTI-TENANT": ["TENANT", "BELONGSTOTENANT"],
        "PERFORMANCE": ["SLOW", "TIMEOUT", "PERFORMANCE"],
        "POLICY": ["POLICY", "VIOLATION", "ADR"],
    }
    for tag, keywords in keyword_tags.items():
        if any(kw in combined for kw in keywords):
            tags.append(tag.lower())
    return tags


def _infer_error_category(completion: dict) -> str:
    violations = " ".join(completion.get("policy_violations", [])).upper()
    if "NORAWQUERIES" in violations or "SQL" in violations:
        return "policy-violation-raw-sql"
    if "SECURITY" in violations or "AUTH" in violations:
        return "security-violation"
    return "runtime-error"


def _extract_reuse_guidance(completion: dict) -> list[str]:
    guidance = [
        "Search this lesson before applying similar repairs.",
        "Check policy_violations list for the exact ADR rule violated.",
    ]
    if completion.get("files_modified"):
        guidance.append(
            f"Files affected: {', '.join(completion['files_modified'][:3])}"
        )
    for suggested in completion.get("next_suggested_tasks", [])[:2]:
        if isinstance(suggested, dict) and suggested.get("title"):
            guidance.append(f"Follow-up task: {suggested['title']}")
    return guidance


def capture_all_uncaptured(dry_run: bool = False) -> int:
    """Scan completions/ and capture lessons for any uncaptured REPAIR tasks."""
    captured = _load_captured_index()
    comp_files = sorted(COMPLETIONS_DIR.glob("REPAIR-*.json"))
    total = 0

    # Group by task_id (keep only latest per task)
    by_task: dict[str, Path] = {}
    for f in comp_files:
        # Extract task_id from filename like REPAIR-20260312173532-649eac-20260313001405.json
        parts = f.stem.rsplit("-", 1)
        task_id = parts[0] if len(parts) == 2 else f.stem
        if task_id not in by_task or f.stat().st_mtime > by_task[task_id].stat().st_mtime:
            by_task[task_id] = f

    print(f"Found {len(by_task)} REPAIR completions, {len(captured)} already captured")

    for task_id, comp_file in by_task.items():
        if task_id in captured:
            continue
        try:
            completion = json.loads(comp_file.read_text(encoding="utf-8"))
            if not dry_run:
                if capture_lesson_from_completion(task_id, completion):
                    total += 1
            else:
                print(f"  [DRY-RUN] Would capture lesson for {task_id}")
                total += 1
        except Exception as e:
            print(f"  ERROR {task_id}: {e}")

    print(f"Captured {total} new lessons.")
    return total


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC Lesson Converter v2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--convert-all",  action="store_true", help="Convert all .md lessons to .json")
    group.add_argument("--capture",      metavar="TASK_ID",   help="Capture lesson for a specific task")
    group.add_argument("--capture-all",  action="store_true", help="Capture all uncaptured REPAIR lessons")
    parser.add_argument("--dry-run",     action="store_true")
    args = parser.parse_args()

    if args.convert_all:
        convert_all_md_lessons(dry_run=args.dry_run)
    elif args.capture:
        capture_lesson_from_completion(args.capture)
    elif args.capture_all:
        capture_all_uncaptured(dry_run=args.dry_run)
