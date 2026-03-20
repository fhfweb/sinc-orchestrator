"""
SINC Tenant Isolation Auto-Patcher

Reads the tenant-isolation-audit.json report and automatically adds
the BelongsToTenant trait to models flagged as likely_tenant_scoped.

Usage:
    python patch_tenant_isolation.py [--dry-run] [--force]

    --dry-run   Show what would be patched, don't modify files
    --force     Patch all missing models, not just likely_tenant_scoped ones

Safety:
    - Only adds the trait if it is NOT already present
    - Backs up each file to .bak before patching
    - Writes a patch report to reports/tenant-isolation-patch-<ts>.json
    - Creates a REPAIR task completion in tasks/completions/
"""

import argparse
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

BASE           = Path(__file__).parent.parent.parent
AUDIT_REPORT   = BASE / "reports" / "tenant-isolation-audit.json"
PATCH_REPORTS  = BASE / "reports"
COMPLETIONS    = BASE / "tasks" / "completions"
TRAIT_CLASS    = "BelongsToTenant"
TRAIT_IMPORT   = "use App\\\\Traits\\\\BelongsToTenant;"
TASK_ID        = "REPAIR-TENANT-ISOLATION-20260314"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load_audit() -> dict:
    if not AUDIT_REPORT.exists():
        raise FileNotFoundError(f"Audit report not found: {AUDIT_REPORT}")
    return json.loads(AUDIT_REPORT.read_text(encoding="utf-8"))


def _already_has_trait(content: str) -> bool:
    """Check if file already uses BelongsToTenant."""
    return "BelongsToTenant" in content


def _find_trait_namespace(project_root: Path) -> str:
    """Auto-detect BelongsToTenant namespace from existing models."""
    # Search models that already use the trait to find the correct import
    for php_file in (project_root / "app" / "Models").glob("*.php"):
        content = php_file.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"use\s+([\w\\]+BelongsToTenant)\s*;", content)
        if m:
            return f"use {m.group(1)};"
    # Default fallback
    return "use App\\Traits\\BelongsToTenant;"


def _find_use_trait_insertion_point(content: str) -> tuple[int, str]:
    """
    Find where to insert 'use BelongsToTenant;' inside the class body.
    Returns (line_index, indentation).
    Inserts after the last 'use TraitName;' line, or after the '{' of the class.
    """
    lines = content.splitlines()

    # Find class declaration line
    class_line_idx = -1
    for i, line in enumerate(lines):
        if re.match(r"\s*(abstract\s+|readonly\s+)?class\s+\w+", line):
            class_line_idx = i
            break

    if class_line_idx == -1:
        return -1, "    "

    # Find the opening brace of the class
    brace_idx = -1
    for i in range(class_line_idx, min(class_line_idx + 10, len(lines))):
        if "{" in lines[i]:
            brace_idx = i
            break

    if brace_idx == -1:
        return -1, "    "

    # Find last 'use Trait;' line after the brace
    last_use_idx = brace_idx
    indent = "    "
    for i in range(brace_idx + 1, min(brace_idx + 30, len(lines))):
        stripped = lines[i].strip()
        if re.match(r"use\s+[\w\\]+(,\s*[\w\\]+)*\s*;", stripped):
            last_use_idx = i
            # capture indentation
            m = re.match(r"(\s+)", lines[i])
            if m:
                indent = m.group(1)
        elif stripped and not stripped.startswith("//") and not stripped.startswith("/*"):
            break  # hit non-use, non-comment line

    return last_use_idx, indent


def patch_file(php_file: Path, trait_import: str, dry_run: bool) -> dict:
    """
    Patch a single PHP model file to add BelongsToTenant.
    Returns a result dict.
    """
    result = {
        "file": str(php_file),
        "model": php_file.stem,
        "action": "none",
        "error": ""
    }

    try:
        content = php_file.read_text(encoding="utf-8", errors="replace")

        if _already_has_trait(content):
            result["action"] = "skipped-already-has-trait"
            return result

        lines = content.splitlines(keepends=True)

        # 1. Add import statement after last 'use Namespace\...' import in file header
        last_import_idx = -1
        for i, line in enumerate(lines):
            if re.match(r"use\s+[\w\\]+\s*;", line.strip()):
                last_import_idx = i
            # Stop looking once we hit the class declaration
            if re.match(r"\s*(abstract\s+)?class\s+\w+", line):
                break

        if last_import_idx >= 0:
            # Check if import already there
            import_line = trait_import + "\n"
            if import_line not in lines:
                lines.insert(last_import_idx + 1, import_line)
                content = "".join(lines)
        else:
            # No existing imports found — add after <?php
            for i, line in enumerate(lines):
                if line.strip().startswith("<?php"):
                    lines.insert(i + 2, trait_import + "\n")
                    content = "".join(lines)
                    break

        # Re-parse after import insertion
        lines = content.splitlines()

        # 2. Add 'use BelongsToTenant;' inside the class body
        insert_idx, indent = _find_use_trait_insertion_point(content)
        if insert_idx == -1:
            result["action"] = "error"
            result["error"] = "Could not find class body insertion point"
            return result

        lines.insert(insert_idx + 1, f"{indent}use {TRAIT_CLASS};")
        new_content = "\n".join(lines) + "\n"

        if dry_run:
            result["action"] = "would-patch"
            return result

        # Backup
        shutil.copy2(php_file, str(php_file) + ".bak")

        # Write
        php_file.write_text(new_content, encoding="utf-8")
        result["action"] = "patched"
        return result

    except Exception as e:
        result["action"] = "error"
        result["error"] = str(e)
        return result


def run(dry_run: bool = False, force: bool = False):
    audit = _load_audit()
    missing = audit.get("models_missing_trait", [])

    # Detect trait namespace from existing models
    project_root = BASE
    trait_import = _find_trait_namespace(project_root)
    print(f"Trait import detected: {trait_import}")

    # Select models to patch
    if force:
        targets = missing
    else:
        targets = [m for m in missing if m.get("likely_tenant_scoped", False)]

    print(f"\nTargets: {len(targets)} models {'(dry-run)' if dry_run else '(live patch)'}")
    print("-" * 60)

    results = []
    patched_count = 0
    skipped_count = 0
    error_count = 0

    for model_info in targets:
        php_path = Path(model_info["file"])
        if not php_path.exists():
            print(f"  MISSING FILE: {model_info['model']}")
            results.append({"model": model_info["model"], "action": "file-not-found", "error": "file missing"})
            error_count += 1
            continue

        res = patch_file(php_path, trait_import, dry_run)
        results.append(res)

        icon = {"patched": "+", "would-patch": "~", "skipped-already-has-trait": "=", "error": "!"}.get(res["action"], "?")
        print(f"  {icon} {res['model']}: {res['action']}" + (f" — {res['error']}" if res["error"] else ""))

        if res["action"] == "patched":
            patched_count += 1
        elif res["action"] == "skipped-already-has-trait":
            skipped_count += 1
        elif res["action"] == "error":
            error_count += 1
        elif res["action"] == "would-patch":
            patched_count += 1

    print("-" * 60)
    print(f"Done: {patched_count} {'would be ' if dry_run else ''}patched, {skipped_count} skipped, {error_count} errors")

    # Write patch report
    ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    report = {
        "generated_at": _now(),
        "dry_run": dry_run,
        "force": force,
        "trait_import": trait_import,
        "targets_count": len(targets),
        "patched": patched_count,
        "skipped": skipped_count,
        "errors": error_count,
        "results": results
    }
    PATCH_REPORTS.mkdir(parents=True, exist_ok=True)
    report_path = PATCH_REPORTS / f"tenant-isolation-patch-{ts}.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nReport written to: {report_path.name}")

    # Write task completion if not dry-run
    if not dry_run and patched_count > 0:
        COMPLETIONS.mkdir(parents=True, exist_ok=True)
        completion = {
            "schema_version": "v3",
            "task_id": TASK_ID,
            "agent_name": "patch-tenant-isolation",
            "status": "done" if error_count == 0 else "partial",
            "summary": (
                f"Added BelongsToTenant trait to {patched_count} models. "
                f"{skipped_count} already had trait. {error_count} errors."
            ),
            "files_modified": [r["file"] for r in results if r["action"] == "patched"],
            "policy_violations": [],
            "validated_at": _now(),
            "next_suggested_tasks": [
                {"id": "TEST-TENANT-ISOLATION", "title": "Run php artisan test to verify tenant isolation patches"}
            ]
        }
        comp_path = COMPLETIONS / f"{TASK_ID}-{ts}.json"
        comp_path.write_text(json.dumps(completion, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Completion written: {comp_path.name}")

    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC Tenant Isolation Auto-Patcher")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be patched, don't modify files")
    parser.add_argument("--force",   action="store_true", help="Patch ALL missing models (not just likely_tenant_scoped)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, force=args.force)
