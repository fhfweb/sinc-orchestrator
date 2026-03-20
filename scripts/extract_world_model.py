import argparse
import json
import re
from collections import defaultdict
from pathlib import Path


EXCLUDED_DIRECTORIES = {
    ".git",
    "node_modules",
    "vendor",
    "dist",
    "build",
    "coverage",
    ".next",
    "out",
    "bin",
    "obj",
    "target",
    ".venv",
    "venv",
    "__pycache__",
    "workspace",
    "infra",
}

PRISMA_SCALARS = {
    "String",
    "Boolean",
    "Int",
    "BigInt",
    "Float",
    "Decimal",
    "DateTime",
    "Json",
    "Bytes",
    "Unsupported",
}

RELATION_TYPE_MAP = {
    "belongsTo": "BELONGS_TO",
    "hasOne": "HAS_ONE",
    "hasMany": "HAS_MANY",
    "belongsToMany": "BELONGS_TO_MANY",
    "morphMany": "MORPH_MANY",
    "morphOne": "MORPH_ONE",
    "morphTo": "MORPH_TO",
    "morphToMany": "MORPH_TO_MANY",
}


def parse_args():
    parser = argparse.ArgumentParser(
        description="Extract domain entities and relationships into a generated world model."
    )
    parser.add_argument("--project-path", required=True, help="Target project root.")
    parser.add_argument("--output-path", required=True, help="Generated Markdown output.")
    parser.add_argument(
        "--json-output-path",
        help="Optional machine-readable JSON output with the extracted model.",
    )
    parser.add_argument("--project-slug", help="Project slug for metadata.")
    return parser.parse_args()


def should_skip_path(path: Path) -> bool:
    return any(part in EXCLUDED_DIRECTORIES for part in path.parts)


def canonical_entity_key(name: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()
    if normalized.endswith("ies"):
        normalized = normalized[:-3] + "y"
    elif normalized.endswith("ses"):
        normalized = normalized[:-2]
    elif normalized.endswith("s") and not normalized.endswith("ss"):
        normalized = normalized[:-1]
    return normalized


def humanize_identifier(value: str) -> str:
    text = value.replace("_", " ").replace("-", " ")
    return re.sub(r"\s+", " ", text).strip().title()


def singular_display_name(name: str) -> str:
    text = re.sub(r"[_\-]+", " ", name).strip()
    if text.lower().endswith("ies"):
        text = text[:-3] + "y"
    elif text.lower().endswith("s") and not text.lower().endswith("ss"):
        text = text[:-1]
    return humanize_identifier(text)


def ensure_entity(catalog: dict, name: str):
    key = canonical_entity_key(name)
    if key not in catalog:
        catalog[key] = {
            "key": key,
            "name": singular_display_name(name),
            "raw_names": set(),
            "sources": set(),
            "files": set(),
            "tables": set(),
            "attributes": set(),
            "relationships": [],
            "confidence_flags": [],
            "lifecycle_hints": set(),
        }
    catalog[key]["raw_names"].add(name)
    return catalog[key]


def add_relationship(catalog: dict, source_name: str, relation_type: str, target_name: str, source_hint: str, file_path: Path, reason: str = ""):
    source_entity = ensure_entity(catalog, source_name)
    ensure_entity(catalog, target_name)
    source_entity["relationships"].append(
        {
            "type": relation_type,
            "target": canonical_entity_key(target_name),
            "target_name": singular_display_name(target_name),
            "source_hint": source_hint,
            "file": file_path.as_posix(),
            "reason": reason,
        }
    )


def parse_prisma_models(catalog: dict, project_root: Path):
    pattern = re.compile(r"model\s+(\w+)\s*\{(.*?)\n\}", re.IGNORECASE | re.DOTALL)
    for file_path in project_root.rglob("schema.prisma"):
        if should_skip_path(file_path.relative_to(project_root)):
            continue

        content = file_path.read_text(encoding="utf-8", errors="ignore")
        for match in pattern.finditer(content):
            model_name = match.group(1)
            block = match.group(2)
            entity = ensure_entity(catalog, model_name)
            entity["sources"].add("prisma")
            entity["files"].add(file_path.as_posix())
            entity["confidence_flags"].append("Prisma model detected")

            for raw_line in block.splitlines():
                line = raw_line.split("//", 1)[0].strip()
                if not line:
                    continue

                parts = re.split(r"\s+", line, maxsplit=2)
                if len(parts) < 2:
                    continue

                field_name = parts[0]
                field_type = parts[1]
                field_type_base = field_type.rstrip("?")
                is_list = field_type_base.endswith("[]")
                if is_list:
                    field_type_base = field_type_base[:-2]

                if field_type_base in PRISMA_SCALARS:
                    entity["attributes"].add(field_name)
                    if field_name in {"deleted_at", "archived_at"}:
                        entity["lifecycle_hints"].add("supports archival or soft deletion")
                    if field_name == "status":
                        entity["lifecycle_hints"].add("status-driven lifecycle")
                    continue

                relation_type = "HAS_MANY" if is_list else "RELATES_TO"
                add_relationship(
                    catalog=catalog,
                    source_name=model_name,
                    relation_type=relation_type,
                    target_name=field_type_base,
                    source_hint="prisma",
                    file_path=file_path,
                    reason=f"Prisma relation via field `{field_name}`",
                )


def parse_sql_files(catalog: dict, project_root: Path):
    create_table_pattern = re.compile(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"\[]?([A-Za-z0-9_.]+)[`\"\]]?\s*\((.*?)\);",
        re.IGNORECASE | re.DOTALL,
    )
    inline_reference_pattern = re.compile(r"REFERENCES\s+[`\"\[]?([A-Za-z0-9_.]+)[`\"\]]?", re.IGNORECASE)
    foreign_key_pattern = re.compile(
        r"FOREIGN\s+KEY\s*\([^)]+\)\s*REFERENCES\s+[`\"\[]?([A-Za-z0-9_.]+)[`\"\]]?",
        re.IGNORECASE,
    )

    for file_path in project_root.rglob("*.sql"):
        relative_path = file_path.relative_to(project_root)
        if should_skip_path(relative_path):
            continue

        content = file_path.read_text(encoding="utf-8", errors="ignore")
        for match in create_table_pattern.finditer(content):
            table_name = match.group(1).split(".")[-1]
            block = match.group(2)
            entity = ensure_entity(catalog, table_name)
            entity["sources"].add("sql")
            entity["files"].add(file_path.as_posix())
            entity["tables"].add(table_name)
            entity["confidence_flags"].append("SQL table detected")

            for raw_line in block.splitlines():
                line = raw_line.strip().rstrip(",")
                if not line:
                    continue

                if line.upper().startswith(("PRIMARY KEY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT")):
                    if "UNIQUE" in line.upper():
                        entity["confidence_flags"].append(f"Constraint: {line}")
                    foreign_match = foreign_key_pattern.search(line)
                    if foreign_match:
                        target_table = foreign_match.group(1).split(".")[-1]
                        add_relationship(
                            catalog=catalog,
                            source_name=table_name,
                            relation_type="DEPENDS_ON",
                            target_name=target_table,
                            source_hint="sql",
                            file_path=file_path,
                            reason="Foreign key constraint",
                        )
                    continue

                column_match = re.match(r"[`\"\[]?([A-Za-z0-9_]+)[`\"\]]?\s+([A-Za-z0-9()]+)", line)
                if not column_match:
                    continue

                column_name = column_match.group(1)
                entity["attributes"].add(column_name)
                if column_name in {"deleted_at", "archived_at"}:
                    entity["lifecycle_hints"].add("supports archival or soft deletion")
                if column_name == "status":
                    entity["lifecycle_hints"].add("status-driven lifecycle")

                inline_reference = inline_reference_pattern.search(line)
                if inline_reference:
                    target_table = inline_reference.group(1).split(".")[-1]
                    add_relationship(
                        catalog=catalog,
                        source_name=table_name,
                        relation_type="DEPENDS_ON",
                        target_name=target_table,
                        source_hint="sql",
                        file_path=file_path,
                        reason=f"Column `{column_name}` references `{target_table}`",
                    )
                elif column_name.endswith("_id"):
                    target_guess = column_name[:-3]
                    add_relationship(
                        catalog=catalog,
                        source_name=table_name,
                        relation_type="RELATES_TO",
                        target_name=target_guess,
                        source_hint="sql",
                        file_path=file_path,
                        reason=f"Column `{column_name}` suggests a foreign key",
                    )


def parse_php_models(catalog: dict, project_root: Path):
    class_pattern = re.compile(r"class\s+([A-Za-z0-9_]+)\s+extends\s+Model", re.IGNORECASE)
    table_pattern = re.compile(r"protected\s+\$table\s*=\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    fillable_pattern = re.compile(r"\$fillable\s*=\s*\[(.*?)\];", re.IGNORECASE | re.DOTALL)
    relationship_pattern = re.compile(
        r"function\s+([A-Za-z0-9_]+)\s*\([^)]*\)\s*\{(.*?)\}",
        re.IGNORECASE | re.DOTALL,
    )
    relation_call_pattern = re.compile(
        r"\$this->(belongsTo|hasOne|hasMany|belongsToMany|morphMany|morphOne|morphTo|morphToMany)\(\s*([A-Za-z0-9_\\]+)::class",
        re.IGNORECASE,
    )
    quoted_string_pattern = re.compile(r"['\"]([^'\"]+)['\"]")

    for file_path in project_root.rglob("*.php"):
        relative_path = file_path.relative_to(project_root)
        if should_skip_path(relative_path):
            continue

        content = file_path.read_text(encoding="utf-8", errors="ignore")
        class_match = class_pattern.search(content)
        if not class_match:
            continue

        model_name = class_match.group(1)
        entity = ensure_entity(catalog, model_name)
        entity["sources"].add("eloquent")
        entity["files"].add(file_path.as_posix())
        entity["confidence_flags"].append("Eloquent model detected")

        table_match = table_pattern.search(content)
        if table_match:
            entity["tables"].add(table_match.group(1))

        fillable_match = fillable_pattern.search(content)
        if fillable_match:
            for attribute in quoted_string_pattern.findall(fillable_match.group(1)):
                entity["attributes"].add(attribute)

        for method_match in relationship_pattern.finditer(content):
            method_body = method_match.group(2)
            relation_match = relation_call_pattern.search(method_body)
            if not relation_match:
                continue

            raw_relation = relation_match.group(1)
            relation_type = RELATION_TYPE_MAP.get(raw_relation, "RELATES_TO")
            related_class = relation_match.group(2).split("\\")[-1]
            add_relationship(
                catalog=catalog,
                source_name=model_name,
                relation_type=relation_type,
                target_name=related_class,
                source_hint="eloquent",
                file_path=file_path,
                reason=f"Eloquent relation via `{raw_relation}`",
            )


def infer_business_rules(catalog: dict):
    rules = []
    for entity in catalog.values():
        for relationship in entity["relationships"]:
            if relationship["type"] == "DEPENDS_ON":
                rules.append(
                    f"{entity['name']} depends on {relationship['target_name']} existing before the relationship can be valid."
                )
            elif relationship["type"] == "BELONGS_TO":
                rules.append(
                    f"{entity['name']} belongs to {relationship['target_name']} according to ORM relations."
                )
            elif relationship["type"] == "HAS_MANY":
                rules.append(
                    f"{entity['name']} owns multiple {relationship['target_name']} records."
                )

        if "status-driven lifecycle" in entity["lifecycle_hints"]:
            rules.append(f"{entity['name']} likely has guarded state transitions via a `status` field.")
        if "supports archival or soft deletion" in entity["lifecycle_hints"]:
            rules.append(f"{entity['name']} likely supports non-destructive removal or archival.")

    deduped = []
    seen = set()
    for rule in rules:
        if rule not in seen:
            deduped.append(rule)
            seen.add(rule)
    return deduped


def parse_project_request(project_root: Path):
    request_path = project_root / "PROJECT_REQUEST.md"
    if not request_path.exists():
        return {}

    sections = {}
    current_key = None
    for line in request_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("## "):
            current_key = stripped[3:].strip().lower().replace(" ", "_")
            sections[current_key] = []
            continue

        if current_key:
            sections[current_key].append(line.rstrip())

    normalized = {}
    for key, values in sections.items():
        text = "\n".join(value for value in values if value.strip()).strip()
        if text:
            normalized[key] = text

    return normalized


def build_summary(catalog: dict):
    source_counts = defaultdict(int)
    relation_count = 0
    for entity in catalog.values():
        relation_count += len(entity["relationships"])
        for source in entity["sources"]:
            source_counts[source] += 1
    return {
        "entity_count": len(catalog),
        "relationship_count": relation_count,
        "source_counts": dict(source_counts),
    }


def build_markdown(project_slug: str, summary: dict, catalog: dict, business_rules, project_request: dict):
    lines = []
    lines.append("# World Model Auto")
    lines.append("")
    lines.append("Generated automatically from repository structure and schema signals.")
    lines.append("This file is a machine-assisted draft. Treat it as high-value context, not as final truth.")
    lines.append("")
    lines.append(f"- Project Slug: {project_slug}")
    lines.append(f"- Entity Count: {summary['entity_count']}")
    lines.append(f"- Relationship Count: {summary['relationship_count']}")
    lines.append("")
    if project_request:
        lines.append("## Project Request Signals")
        for key in [
            "name",
            "product_goal",
            "delivery_surface",
            "preferred_stack",
            "preferred_database",
            "docker_required",
            "deployment_target",
            "constraints",
            "notes",
        ]:
            if key in project_request:
                label = humanize_identifier(key)
                lines.append(f"- {label}: {project_request[key]}")
        lines.append("")
    lines.append("## Detection Summary")
    if summary["source_counts"]:
        for source, count in sorted(summary["source_counts"].items()):
            lines.append(f"- {source}: {count} entities")
    else:
        lines.append("- No entity sources detected.")
    lines.append("")
    lines.append("## Entity Catalog")
    lines.append("| Entity | Sources | Tables | Attributes |")
    lines.append("|--------|---------|--------|------------|")
    if catalog:
        for entity in sorted(catalog.values(), key=lambda item: item["name"].lower()):
            lines.append(
                f"| {entity['name']} | {', '.join(sorted(entity['sources'])) or '-'} | "
                f"{', '.join(sorted(entity['tables'])) or '-'} | "
                f"{', '.join(sorted(entity['attributes'])) or '-'} |"
            )
    else:
        lines.append("| - | - | - | - |")
    lines.append("")
    lines.append("## Detailed Entities")
    if not catalog:
        lines.append("- No entities were extracted. This usually means the project is still greenfield or uses unsupported modeling patterns.")
    else:
        for entity in sorted(catalog.values(), key=lambda item: item["name"].lower()):
            lines.append(f"### {entity['name']}")
            lines.append(f"- Raw Names: {', '.join(sorted(entity['raw_names']))}")
            lines.append(f"- Sources: {', '.join(sorted(entity['sources'])) or 'unknown'}")
            lines.append(f"- Files: {', '.join(sorted(entity['files'])) or 'unknown'}")
            lines.append(f"- Tables: {', '.join(sorted(entity['tables'])) or 'unknown'}")
            lines.append(f"- Attributes: {', '.join(sorted(entity['attributes'])) or 'unknown'}")
            if entity["lifecycle_hints"]:
                lines.append(f"- Lifecycle Hints: {', '.join(sorted(entity['lifecycle_hints']))}")
            if entity["confidence_flags"]:
                lines.append(f"- Confidence Signals: {', '.join(sorted(set(entity['confidence_flags'])))}")
            if entity["relationships"]:
                lines.append("- Relationships:")
                for relationship in entity["relationships"]:
                    lines.append(
                        f"  - {relationship['type']} -> {relationship['target_name']} "
                        f"({relationship['source_hint']}; {relationship['reason']})"
                    )
            else:
                lines.append("- Relationships: none detected")
            lines.append("")
    lines.append("## Candidate Business Rules")
    if business_rules:
        for rule in business_rules:
            lines.append(f"- {rule}")
    else:
        lines.append("- No business rules inferred from structure alone.")
    lines.append("")
    lines.append("## Open Questions")
    lines.append("- Validate the extracted entities against the real business domain.")
    lines.append("- Confirm which inferred relationships are actual business invariants.")
    lines.append("- Add user journey, failure modes, and constraints manually where code cannot reveal them.")
    return "\n".join(lines).strip() + "\n"


def serialize_catalog(catalog: dict):
    result = []
    for entity in sorted(catalog.values(), key=lambda item: item["name"].lower()):
        result.append(
            {
                "key": entity["key"],
                "name": entity["name"],
                "raw_names": sorted(entity["raw_names"]),
                "sources": sorted(entity["sources"]),
                "files": sorted(entity["files"]),
                "tables": sorted(entity["tables"]),
                "attributes": sorted(entity["attributes"]),
                "lifecycle_hints": sorted(entity["lifecycle_hints"]),
                "confidence_flags": sorted(set(entity["confidence_flags"])),
                "relationships": entity["relationships"],
            }
        )
    return result


def main():
    args = parse_args()
    project_root = Path(args.project_path).resolve()
    output_path = Path(args.output_path).resolve()
    project_slug = args.project_slug or project_root.name.lower()

    catalog = {}
    parse_prisma_models(catalog, project_root)
    parse_sql_files(catalog, project_root)
    parse_php_models(catalog, project_root)
    project_request = parse_project_request(project_root)

    summary = build_summary(catalog)
    business_rules = infer_business_rules(catalog)
    markdown = build_markdown(project_slug, summary, catalog, business_rules, project_request)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(markdown, encoding="utf-8")

    json_summary = {
        "project_slug": project_slug,
        "summary": summary,
        "project_request": project_request,
        "entities": serialize_catalog(catalog),
        "business_rules": business_rules,
        "output_path": str(output_path),
    }

    if args.json_output_path:
        json_output_path = Path(args.json_output_path).resolve()
        json_output_path.parent.mkdir(parents=True, exist_ok=True)
        json_output_path.write_text(json.dumps(json_summary, indent=2), encoding="utf-8")

    print(json.dumps(json_summary, indent=2))


if __name__ == "__main__":
    main()
