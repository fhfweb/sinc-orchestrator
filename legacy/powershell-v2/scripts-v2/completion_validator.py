"""
SINC Orchestrator Completion Validator v2
Validates agent completion payloads against the mandatory schema
and writes structured completion files.

Required schema:
    task_id             str       - task identifier
    agent_name          str       - agent that completed the task
    status              str       - success | partial | failed
    files_modified      list[str] - files changed (can be empty)
    tests_passed        bool      - whether tests passed
    policy_violations   list[str] - any policy violations detected
    next_suggested_tasks list[dict]- tasks the agent suggests adding to backlog
    summary             str       - human-readable summary

Usage:
    python completion_validator.py --file <path>
    python completion_validator.py --scan  (scan completions/ directory)
    python completion_validator.py --validate-stdin
"""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE          = Path(__file__).parent.parent.parent
COMPLETIONS   = BASE / "tasks" / "completions"
LOGS_DIR      = BASE / "logs"

REQUIRED_FIELDS = {
    "task_id":              str,
    "agent_name":           str,
    "status":               str,
    "files_modified":       list,
    "tests_passed":         bool,
    "policy_violations":    list,
    "next_suggested_tasks": list,
    "summary":              str,
}

VALID_STATUSES = {"success", "partial", "failed"}


class ValidationError(Exception):
    pass


def validate(payload: dict) -> list[str]:
    """
    Validate a completion payload.
    Returns list of error strings (empty = valid).
    """
    errors = []

    # Check required fields
    for field, expected_type in REQUIRED_FIELDS.items():
        if field not in payload:
            errors.append(f"Missing required field: '{field}'")
            continue
        val = payload[field]
        if not isinstance(val, expected_type):
            errors.append(
                f"Field '{field}' must be {expected_type.__name__}, "
                f"got {type(val).__name__}"
            )

    # Check status values
    if "status" in payload and payload["status"] not in VALID_STATUSES:
        errors.append(
            f"Invalid status '{payload['status']}'. "
            f"Must be one of: {sorted(VALID_STATUSES)}"
        )

    # Validate next_suggested_tasks structure
    for i, suggested in enumerate(payload.get("next_suggested_tasks", [])):
        if not isinstance(suggested, dict):
            errors.append(f"next_suggested_tasks[{i}] must be an object")
            continue
        if "title" not in suggested and "description" not in suggested:
            errors.append(
                f"next_suggested_tasks[{i}] must have 'title' or 'description'"
            )

    return errors


def normalize(payload: dict) -> dict:
    """
    Fill in missing optional fields with safe defaults to ensure
    downstream consumers don't fail on missing keys.
    """
    defaults = {
        "schema_version":       "v3-completion",
        "files_modified":       [],
        "tests_passed":         False,
        "policy_violations":    [],
        "next_suggested_tasks": [],
        "summary":              "",
        "tool_calls":           [],
        "local_library_candidates": [],
        "library_decision": {
            "selected_option":  "not-applicable",
            "justification":    "",
            "selected_libraries": [],
            "rejected_libraries": [],
        },
        "validated_at": datetime.now(timezone.utc).isoformat(),
    }
    for k, v in defaults.items():
        payload.setdefault(k, v)
    return payload


def validate_file(path: Path, fix: bool = False) -> bool:
    """Validate a single completion file. Returns True if valid."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"  ERROR {path.name}: Invalid JSON — {e}")
        return False

    errors = validate(data)
    if errors:
        print(f"  INVALID {path.name}:")
        for err in errors:
            print(f"    - {err}")
        if fix:
            data = normalize(data)
            # Fill in plausible defaults for missing required fields
            if "task_id" not in data and path.stem:
                data["task_id"] = path.stem.split("-2026")[0]
            if "agent_name" not in data:
                data["agent_name"] = "unknown"
            if "status" not in data or data["status"] not in VALID_STATUSES:
                data["status"] = "partial"
            if "summary" not in data:
                data["summary"] = f"Auto-fixed completion for {data.get('task_id', '?')}"

            remaining = validate(data)
            if not remaining:
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"    FIXED {path.name}")
                return True
            else:
                print(f"    COULD NOT FIX: {remaining}")
        return False
    else:
        if fix:
            # Normalize even valid payloads to add missing optional fields
            normalized = normalize(data)
            if normalized != data:
                path.write_text(json.dumps(normalized, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  OK {path.name}")
        return True


def scan_completions(fix: bool = False) -> tuple[int, int]:
    """Scan all completion files. Returns (valid, invalid)."""
    if not COMPLETIONS.exists():
        print("  No completions directory found")
        return 0, 0

    files = sorted(COMPLETIONS.glob("*.json"))
    print(f"Scanning {len(files)} completion files...")
    valid_count = 0
    invalid_count = 0

    for f in files:
        if validate_file(f, fix=fix):
            valid_count += 1
        else:
            invalid_count += 1

    print(f"\nResults: {valid_count} valid, {invalid_count} invalid")
    return valid_count, invalid_count


def validate_stdin():
    """Read completion JSON from stdin and validate."""
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)

    errors = validate(data)
    if errors:
        print("INVALID:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    normalized = normalize(data)
    print(json.dumps(normalized, indent=2))
    sys.exit(0)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC Completion Validator v2")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file",           metavar="PATH", help="Validate a single completion file")
    group.add_argument("--scan",           action="store_true", help="Scan completions/ directory")
    group.add_argument("--validate-stdin", action="store_true", help="Read JSON from stdin")
    parser.add_argument("--fix",           action="store_true", help="Auto-fix invalid completions")
    args = parser.parse_args()

    if args.validate_stdin:
        validate_stdin()
    elif args.file:
        ok = validate_file(Path(args.file), fix=args.fix)
        sys.exit(0 if ok else 1)
    elif args.scan:
        valid, invalid = scan_completions(fix=args.fix)
        sys.exit(0 if invalid == 0 else 1)
