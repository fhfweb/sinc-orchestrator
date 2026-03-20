#!/usr/bin/env python3
"""
Group F: BelongsToTenant Security Audit
Automated multi-tenant isolation audit for SINC Laravel application.

Usage:
    python audit_tenant_isolation.py [--output-dir PATH] [--create-repair-task]
"""

import os
import re
import json
import argparse
from datetime import datetime, timezone
from pathlib import Path

# ─── Configuration ────────────────────────────────────────────────────────────

SINC_ROOT = Path("g:/Fernando/project0/workspace/projects/SINC")
MODELS_DIR = SINC_ROOT / "app" / "Models"
APP_DIR = SINC_ROOT / "app"

SCRIPT_DIR = Path(__file__).parent
DEFAULT_OUTPUT_DIR = SCRIPT_DIR.parent.parent / "reports"

# Models that legitimately should NOT have BelongsToTenant
# Tenant: the root entity itself
# User: has the trait already (authenticating entity)
# Plan/SaaSProduct/SaaSModule/Module/Product: SaaS catalog — global definitions
# Representative: SaaS-level sales rep entity
# Coupon: SaaS-level promotional coupon
# SystemSetting: global key-value store
# Announcement: platform-wide announcements
# Expense/MarketingSpend: internal SaaS operational cost tracking
EXEMPT_MODELS = {
    "Tenant",
    "Plan",
    "SaaSProduct",
    "SaaSModule",
    "Module",
    "Product",
    "Representative",
    "Coupon",
    "SystemSetting",
    "Announcement",
    "Expense",
    "MarketingSpend",
    "WebhookLog",      # Gateway-level platform log (confirm if tenant scoping needed)
}

# Known false-positive patterns for raw SQL (transaction wrappers, not raw queries)
RAW_SQL_SAFE_PATTERNS = [
    r"DB::transaction\s*\(",  # transaction() is fine, it's a wrapper
    r"DB::table\s*\(",         # Eloquent query builder — still scoped? No, this bypasses global scopes
    r"\bDB::beginTransaction",
    r"\bDB::commit\(",
    r"\bDB::rollback\(",
    r"\bDB::listen\(",
    r"\bDB::connection\(",
]

# Patterns that truly bypass the global scope (raw SQL)
RAW_SQL_DANGER_PATTERNS = [
    (r"DB::statement\s*\(", "DB::statement"),
    (r"DB::unprepared\s*\(", "DB::unprepared"),
    (r"DB::select\s*\(", "DB::select"),
    (r"DB::insert\s*\(", "DB::insert"),
    (r"DB::update\s*\(", "DB::update"),
    (r"DB::delete\s*\(", "DB::delete"),
    (r"DB::affectingStatement\s*\(", "DB::affectingStatement"),
    (r"PDO\s*->\s*query\s*\(", "PDO->query"),
    (r"PDO\s*->\s*exec\s*\(", "PDO->exec"),
    (r"\\\\DB::(statement|select|insert|update|delete|unprepared|affectingStatement)\s*\(", r"\DB:: raw call"),
]

# ─── Helpers ──────────────────────────────────────────────────────────────────

def read_file_lines(path: Path):
    """Return list of (lineno, text) for a file, handling encoding gracefully."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return list(enumerate(f.readlines(), start=1))
    except Exception:
        return []


def file_content(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


# ─── Part 1: Model Analysis ───────────────────────────────────────────────────

def analyze_models():
    """
    Scan all PHP models in MODELS_DIR.
    Returns:
        models_with_trait:   list of model names
        models_without_trait: list of dicts {model, file, likely_tenant_scoped}
        models_exempt:       list of model names
    """
    model_files = [f for f in MODELS_DIR.glob("*.php") if f.is_file()]

    models_with_trait = []
    models_without_trait = []
    models_exempt = []

    for php_file in sorted(model_files):
        content = file_content(php_file)
        model_name = php_file.stem  # filename without .php

        has_trait = bool(re.search(r"use\s+BelongsToTenant\b", content))
        has_tenant_id_fillable = bool(re.search(r"['\"]tenant_id['\"]", content))
        has_tenant_relation = bool(re.search(r"->belongsTo\s*\(\s*Tenant::class\b|Tenant::class", content))
        references_tenant = has_tenant_id_fillable or has_tenant_relation

        if model_name in EXEMPT_MODELS:
            models_exempt.append(model_name)
        elif has_trait:
            models_with_trait.append(model_name)
        else:
            # Determine if this model is LIKELY tenant-scoped
            likely_scoped = references_tenant
            models_without_trait.append({
                "model": model_name,
                "file": str(php_file),
                "likely_tenant_scoped": likely_scoped,
                "has_tenant_id_fillable": has_tenant_id_fillable,
                "has_tenant_relation": has_tenant_relation,
            })

    return models_with_trait, models_without_trait, models_exempt


# ─── Part 2: isSuperAdmin Bypasses ───────────────────────────────────────────

def scan_super_admin_bypasses():
    """
    Find all PHP files that call isSuperAdmin() or check is_super_admin.
    Flag instances inside WHERE clauses or scopes.
    Returns list of dicts {file, line, context, is_suspicious}.
    """
    results = []
    pattern = re.compile(r"isSuperAdmin\(\)|is_super_admin", re.IGNORECASE)

    # Patterns that indicate a scope/WHERE bypass
    scope_indicators = re.compile(
        r"(->where\b|addGlobalScope|withoutGlobalScope|->scope|Builder\s*\$builder|"
        r"global.*scope|scope.*bypass|tenant.*filter|filter.*tenant)",
        re.IGNORECASE,
    )

    php_files = list(APP_DIR.rglob("*.php"))

    for php_file in sorted(php_files):
        lines = read_file_lines(php_file)
        for lineno, line_text in lines:
            if pattern.search(line_text):
                # Grab surrounding context (2 lines before and after)
                start = max(0, lineno - 3)
                end = min(len(lines), lineno + 2)
                context_lines = [l[1].rstrip() for l in lines[start:end]]
                context_block = " | ".join(context_lines)

                # Check if this bypass appears near a scope/where clause
                context_window = "".join(l[1] for l in lines[max(0, lineno-5):min(len(lines), lineno+5)])
                is_suspicious = bool(scope_indicators.search(context_window))

                results.append({
                    "file": str(php_file),
                    "line": lineno,
                    "context": line_text.strip(),
                    "is_suspicious": is_suspicious,
                })

    return results


# ─── Part 3: Raw SQL Bypasses ─────────────────────────────────────────────────

def scan_raw_sql_bypasses():
    """
    Find PHP files that use DB::statement, DB::select, DB::insert, etc.
    or PDO->query / PDO->exec — these bypass the Eloquent global scope.
    Returns list of dicts {file, line, pattern, context}.
    """
    results = []
    php_files = list(APP_DIR.rglob("*.php"))

    compiled_patterns = [
        (re.compile(pat, re.IGNORECASE), label)
        for pat, label in RAW_SQL_DANGER_PATTERNS
    ]

    for php_file in sorted(php_files):
        lines = read_file_lines(php_file)
        for lineno, line_text in lines:
            for compiled_re, label in compiled_patterns:
                if compiled_re.search(line_text):
                    results.append({
                        "file": str(php_file),
                        "line": lineno,
                        "pattern": label,
                        "context": line_text.strip(),
                    })
                    break  # Only report once per line

    return results


# ─── Part 4: Risk Scoring ─────────────────────────────────────────────────────

def compute_risk(models_missing, raw_sql_bypasses):
    """
    Compute overall risk level.
    critical  — missing trait models with likely_tenant_scoped=True AND raw SQL > 3
    high      — missing trait models with likely_tenant_scoped=True OR raw SQL > 3
    medium    — some missing trait models (not scoped) or moderate raw SQL
    low       — everything clean
    """
    critical_missing = [m for m in models_missing if m["likely_tenant_scoped"]]
    raw_count = len(raw_sql_bypasses)

    if len(critical_missing) > 0 and raw_count > 3:
        return "critical"
    elif len(critical_missing) > 0 or raw_count > 3:
        return "high"
    elif len(models_missing) > 0 or raw_count > 0:
        return "medium"
    else:
        return "low"


# ─── Part 5: Recommendations ──────────────────────────────────────────────────

def build_recommendations(models_missing, super_admin_bypasses, raw_sql_bypasses, exempt_models):
    recs = []

    critical_missing = [m for m in models_missing if m["likely_tenant_scoped"]]
    if critical_missing:
        names = ", ".join(m["model"] for m in critical_missing)
        recs.append(
            f"CRITICAL: Add `use BelongsToTenant;` trait to these likely tenant-scoped models: {names}. "
            "Without this trait, Eloquent queries will return records across ALL tenants."
        )

    non_critical_missing = [m for m in models_missing if not m["likely_tenant_scoped"]]
    if non_critical_missing:
        names = ", ".join(m["model"] for m in non_critical_missing)
        recs.append(
            f"REVIEW: The following models do not use BelongsToTenant and have no obvious tenant references — "
            f"confirm they are truly global/system models or add the trait: {names}"
        )

    suspicious_bypasses = [b for b in super_admin_bypasses if b.get("is_suspicious")]
    if suspicious_bypasses:
        recs.append(
            f"WARNING: {len(suspicious_bypasses)} isSuperAdmin() check(s) appear near WHERE/scope logic. "
            "Review to ensure super-admin bypass is intentional and does not expose unintended cross-tenant data."
        )

    if len(raw_sql_bypasses) > 3:
        recs.append(
            f"HIGH: {len(raw_sql_bypasses)} raw SQL call(s) detected (DB::select, DB::statement, etc.). "
            "These bypass the BelongsToTenant global scope. Each must manually filter by tenant_id "
            "or be wrapped in a tenant-scoped context."
        )
    elif len(raw_sql_bypasses) > 0:
        recs.append(
            f"MEDIUM: {len(raw_sql_bypasses)} raw SQL call(s) detected. "
            "Ensure each one explicitly filters by tenant_id where appropriate."
        )

    if not recs:
        recs.append("No critical issues found. Tenant isolation appears well-implemented.")

    recs.append(
        "BEST PRACTICE: Register a middleware that calls TenantScope::setCurrentTenantId() for all "
        "background jobs/queued tasks to ensure tenant isolation outside of HTTP auth context."
    )

    return recs


# ─── Part 6: Repair Task in PostgreSQL ───────────────────────────────────────

def create_repair_task(summary: dict):
    """
    Insert a REPAIR task into the orchestrator PostgreSQL DB.
    Only called when --create-repair-task flag is passed and conditions are met.
    """
    try:
        import psycopg2
    except ImportError:
        print("[REPAIR TASK] psycopg2 not available — skipping DB insert.")
        return False

    db_password = os.environ.get("ORCH_DB_PASSWORD", "")
    task_id = f"REPAIR-TENANT-ISOLATION-{datetime.now(timezone.utc).strftime('%Y%m%d')}"

    try:
        conn = psycopg2.connect(
            host="localhost",
            port=5434,
            dbname="orchestrator_tasks",
            user="orchestrator",
            password=db_password,
            connect_timeout=5,
        )
        cur = conn.cursor()

        # Try to create table if not exists (idempotent)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                priority TEXT NOT NULL DEFAULT 'high',
                payload JSONB,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)

        payload = json.dumps({
            "source": "audit_tenant_isolation",
            "summary": summary,
            "instructions": (
                "Add BelongsToTenant trait to all models with likely_tenant_scoped=true. "
                "Review all raw SQL calls and add manual tenant_id filtering."
            ),
        })

        cur.execute(
            """
            INSERT INTO tasks (id, type, status, priority, payload)
            VALUES (%s, %s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE
              SET status = EXCLUDED.status,
                  payload = EXCLUDED.payload,
                  updated_at = NOW()
            """,
            (task_id, "REPAIR", "pending", "critical", payload),
        )

        conn.commit()
        cur.close()
        conn.close()
        print(f"[REPAIR TASK] Inserted task {task_id} into orchestrator DB.")
        return True

    except Exception as e:
        print(f"[REPAIR TASK] Could not insert into orchestrator DB: {e}")
        return False


# ─── Part 7: Report Generation ────────────────────────────────────────────────

def generate_markdown(report: dict, output_path: Path):
    now = report["generated_at"]
    s = report["summary"]
    risk = s["risk_level"].upper()

    risk_emoji = {
        "LOW": "GREEN - LOW",
        "MEDIUM": "YELLOW - MEDIUM",
        "HIGH": "ORANGE - HIGH",
        "CRITICAL": "RED - CRITICAL",
    }.get(risk, risk)

    lines = [
        "# Tenant Isolation Security Audit",
        f"",
        f"**Generated:** {now}  ",
        f"**Risk Level:** {risk_emoji}",
        f"",
        "## Summary",
        "",
        f"| Metric | Count |",
        f"|--------|-------|",
        f"| Total Models Scanned | {s['total_models']} |",
        f"| Models WITH BelongsToTenant | {s['models_with_trait']} |",
        f"| Models MISSING BelongsToTenant | {s['models_missing_trait']} |",
        f"| Exempt Models (by design) | {s['models_exempt']} |",
        f"| isSuperAdmin() Bypass Occurrences | {s['super_admin_bypass_count']} |",
        f"| Raw SQL Bypass Occurrences | {s['raw_sql_bypass_count']} |",
        "",
    ]

    # Missing trait models
    lines += ["## Models Missing BelongsToTenant Trait", ""]
    missing = report.get("models_missing_trait", [])
    if missing:
        lines.append("| Model | Likely Tenant-Scoped | File |")
        lines.append("|-------|---------------------|------|")
        for m in missing:
            scoped = "YES (RISK)" if m["likely_tenant_scoped"] else "Uncertain"
            short_file = Path(m["file"]).name
            lines.append(f"| `{m['model']}` | {scoped} | `{short_file}` |")
    else:
        lines.append("_All non-exempt models use the BelongsToTenant trait._")
    lines.append("")

    # Exempt models
    lines += ["## Exempt Models (No BelongsToTenant Expected)", ""]
    exempt = report.get("models_exempt", [])
    lines.append(", ".join(f"`{m}`" for m in sorted(exempt)) if exempt else "_None_")
    lines.append("")

    # isSuperAdmin bypasses
    lines += ["## isSuperAdmin() Bypass Occurrences", ""]
    sa_bypasses = report.get("super_admin_bypasses", [])
    suspicious = [b for b in sa_bypasses if b.get("is_suspicious")]
    if suspicious:
        lines.append(f"> WARNING: {len(suspicious)} occurrence(s) appear near scope/WHERE logic and may warrant review.")
        lines.append("")
    lines.append(f"Total occurrences: **{len(sa_bypasses)}** (suspicious: **{len(suspicious)}**)")
    lines.append("")
    if sa_bypasses:
        lines.append("| File | Line | Suspicious | Context |")
        lines.append("|------|------|-----------|---------|")
        for b in sa_bypasses[:30]:  # Limit table to 30 rows
            short_file = Path(b["file"]).relative_to(SINC_ROOT) if SINC_ROOT in Path(b["file"]).parents else Path(b["file"]).name
            flag = "YES" if b.get("is_suspicious") else "-"
            ctx = b["context"][:80].replace("|", "/")
            lines.append(f"| `{short_file}` | {b['line']} | {flag} | `{ctx}` |")
        if len(sa_bypasses) > 30:
            lines.append(f"| ... | ... | ... | _{len(sa_bypasses)-30} more rows in JSON report_ |")
    lines.append("")

    # Raw SQL bypasses
    lines += ["## Raw SQL Bypasses (Bypass Eloquent Global Scope)", ""]
    raw_bypasses = report.get("raw_sql_bypasses", [])
    if raw_bypasses:
        lines.append(f"Total raw SQL calls found: **{len(raw_bypasses)}**")
        lines.append("")
        lines.append("| File | Line | Pattern | Context |")
        lines.append("|------|------|---------|---------|")
        for b in raw_bypasses[:40]:
            try:
                short_file = Path(b["file"]).relative_to(SINC_ROOT)
            except ValueError:
                short_file = Path(b["file"]).name
            ctx = b["context"][:80].replace("|", "/")
            lines.append(f"| `{short_file}` | {b['line']} | `{b['pattern']}` | `{ctx}` |")
        if len(raw_bypasses) > 40:
            lines.append(f"| ... | ... | ... | _{len(raw_bypasses)-40} more rows in JSON report_ |")
    else:
        lines.append("_No raw SQL bypasses detected._")
    lines.append("")

    # Recommendations
    lines += ["## Recommendations", ""]
    for i, rec in enumerate(report.get("recommendations", []), 1):
        lines.append(f"{i}. {rec}")
    lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[REPORT] Markdown written to: {output_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="SINC Tenant Isolation Security Audit")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Directory to write reports into")
    parser.add_argument("--create-repair-task", action="store_true",
                        help="Insert a REPAIR task into the orchestrator PostgreSQL DB if critical findings exist")
    args = parser.parse_args()

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=" * 64)
    print("  SINC Tenant Isolation Security Audit")
    print(f"  Scanning: {SINC_ROOT}")
    print("=" * 64)

    # ── Step 1: Model scan ──
    print("\n[1/4] Scanning Laravel models...")
    models_with_trait, models_missing, models_exempt = analyze_models()

    total_models = len(models_with_trait) + len(models_missing) + len(models_exempt)
    print(f"      Total models:       {total_models}")
    print(f"      With trait:         {len(models_with_trait)}")
    print(f"      Missing trait:      {len(models_missing)}")
    print(f"      Exempt:             {len(models_exempt)}")

    # ── Step 2: isSuperAdmin scan ──
    print("\n[2/4] Scanning isSuperAdmin() bypasses...")
    super_admin_bypasses = scan_super_admin_bypasses()
    suspicious_count = len([b for b in super_admin_bypasses if b.get("is_suspicious")])
    print(f"      Total occurrences:  {len(super_admin_bypasses)}")
    print(f"      Suspicious (near WHERE/scope): {suspicious_count}")

    # ── Step 3: Raw SQL scan ──
    print("\n[3/4] Scanning raw SQL bypasses...")
    raw_sql_bypasses = scan_raw_sql_bypasses()
    print(f"      Raw SQL calls found: {len(raw_sql_bypasses)}")

    # ── Step 4: Build report ──
    print("\n[4/4] Building reports...")

    risk_level = compute_risk(models_missing, raw_sql_bypasses)
    recommendations = build_recommendations(
        models_missing, super_admin_bypasses, raw_sql_bypasses, models_exempt
    )

    summary = {
        "total_models": total_models,
        "models_with_trait": len(models_with_trait),
        "models_missing_trait": len(models_missing),
        "models_exempt": len(models_exempt),
        "super_admin_bypass_count": len(super_admin_bypasses),
        "raw_sql_bypass_count": len(raw_sql_bypasses),
        "risk_level": risk_level,
    }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "models_with_trait": sorted(models_with_trait),
        "models_missing_trait": models_missing,
        "models_exempt": sorted(models_exempt),
        "super_admin_bypasses": super_admin_bypasses,
        "raw_sql_bypasses": raw_sql_bypasses,
        "recommendations": recommendations,
    }

    # Write JSON
    json_path = output_dir / "tenant-isolation-audit.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[REPORT] JSON written to: {json_path}")

    # Write Markdown
    md_path = output_dir / "tenant-isolation-audit.md"
    generate_markdown(report, md_path)

    # ── Optional: Repair task ──
    should_create_repair = (
        args.create_repair_task
        and (len(models_missing) > 0 or len(raw_sql_bypasses) > 3)
    )
    if should_create_repair:
        print("\n[REPAIR] Creating repair task in orchestrator DB...")
        create_repair_task(summary)
    elif args.create_repair_task:
        print("\n[REPAIR] No critical findings — repair task not required.")

    # ── Final summary ──
    print("\n" + "=" * 64)
    print(f"  RISK LEVEL: {risk_level.upper()}")
    print("=" * 64)

    critical_missing = [m for m in models_missing if m["likely_tenant_scoped"]]
    if critical_missing:
        print(f"\n  CRITICAL: {len(critical_missing)} model(s) missing trait (tenant-scoped):")
        for m in critical_missing:
            print(f"    - {m['model']}")

    if len(models_missing) > len(critical_missing):
        uncertain = [m for m in models_missing if not m["likely_tenant_scoped"]]
        print(f"\n  REVIEW: {len(uncertain)} model(s) missing trait (uncertain scope):")
        for m in uncertain:
            print(f"    - {m['model']}")

    print(f"\n  Reports:")
    print(f"    JSON: {json_path}")
    print(f"    MD:   {md_path}")
    print()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
