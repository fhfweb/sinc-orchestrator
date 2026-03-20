import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path
import sys

import requests

try:
    from qdrant_client import QdrantClient
    from qdrant_client.http import models as qdrant_models
except ImportError:
    QdrantClient = None
    qdrant_models = None

try:
    from neo4j import GraphDatabase
except ImportError:
    GraphDatabase = None


NODE_METADATA_PATTERN = re.compile(r"^##\s+([^:]+):\s*(.+?)\s*$")
SECTION_HEADER_PATTERN = re.compile(r"^##\s+(.+?)\s*$")
RELATIONSHIP_LINE_PATTERN = re.compile(
    r"^\s*\[?([A-Za-z0-9_.:/\\-]+)\]?\s*--\[\s*([A-Za-z0-9_]+)\s*\]-->\s*\[?([A-Za-z0-9_.:/\\-]+)\]?\s*$"
)
INLINE_LINK_TYPES = {"depends_on", "used_by", "related_to", "implements", "replaces", "extends", "conflicts_with"}
BIDIRECTIONAL_RELATIONSHIP_TYPES = {"RELATED_TO", "CONFLICTS_WITH"}
DEFAULT_NODE_LABEL = "MemoryNode"
ALLOWED_NODE_LABELS = {
    "Feature",
    "Architecture",
    "Entity",
    "Decision",
    "Task",
    "Integration",
    "Bug",
    "Experiment",
    "Metric",
    "File",
    "Module",
    "Agent",
    "Code",
    "Class",
    "Function",
    "Call",
    "Route",
    "Table",
    "Column",
    "Index",
    "Incident",
    "Requirement",
    "Risk",
    "Test",
    "Migration",
}
REQUIRED_QDRANT_PAYLOAD_FIELDS = ("project_slug", "node_type", "summary", "details")
PROJECT_SLUG_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")
GLOBAL_NODE_TYPES_DEFAULT = "bug,decision,experiment,integration,metric,pattern,task,architecture"
GLOBAL_SENSITIVE_KEYWORDS = {
    "password",
    "senha",
    "secret",
    "api_key",
    "token",
    "cpf",
    "rg",
    "prontuario",
    "medical_record",
    "private_notes",
    "pii",
    "phi",
    "health_data",
}
GLOBAL_REDACT_PATTERNS = (
    re.compile(r"\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b"),  # CPF
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),  # email
    re.compile(r"\b(?:\+?55\s?)?(?:\(?\d{2}\)?\s?)?(?:9?\d{4})-?\d{4}\b"),  # BR phone
)


def parse_args():
    parser = argparse.ArgumentParser(description="Sync Markdown memory nodes into Qdrant and Neo4j.")
    parser.add_argument("--project-slug", required=True)
    parser.add_argument("--project-root", default="")
    parser.add_argument("--memory-dir", default="memory_graph/nodes")
    parser.add_argument("--relationships-path", default="memory_graph/edges/relationships.md")
    parser.add_argument("--dependency-graph-path", default="")
    parser.add_argument("--world-model-json-path", default="")
    parser.add_argument("--ast-graph-path", default="")
    parser.add_argument("--task-dag-path", default="")
    parser.add_argument("--task-completions-dir", default="")
    parser.add_argument("--collection-prefix", default=os.getenv("QDRANT_COLLECTION_PREFIX", ""))
    parser.add_argument("--qdrant-host", default=os.getenv("QDRANT_HOST", "localhost"))
    parser.add_argument("--qdrant-port", type=int, default=int(os.getenv("QDRANT_PORT", "6333")))
    parser.add_argument("--qdrant-vector-size", type=int, default=int(os.getenv("QDRANT_VECTOR_SIZE", "768")))
    parser.add_argument("--qdrant-upsert-batch-size", type=int, default=int(os.getenv("QDRANT_UPSERT_BATCH_SIZE", "512")))
    parser.add_argument(
        "--qdrant-incremental-sync",
        action="store_true",
        default=str(os.getenv("QDRANT_INCREMENTAL_SYNC", "1")).strip().lower() in {"1", "true", "yes", "on"},
    )
    parser.add_argument("--qdrant-disable-incremental-sync", action="store_true")
    parser.add_argument(
        "--qdrant-prune-orphans",
        action="store_true",
        default=str(os.getenv("QDRANT_PRUNE_ORPHANS", "0")).strip().lower() in {"1", "true", "yes", "on"},
    )
    parser.add_argument(
        "--qdrant-index-fields",
        default=os.getenv(
            "QDRANT_PAYLOAD_INDEX_FIELDS",
            "project_slug,node_type,module_name,relative_path,record_scope,record_type,namespace,source_project_slug",
        ),
    )
    parser.add_argument(
        "--qdrant-disable-payload-indexes",
        action="store_true",
        default=str(os.getenv("QDRANT_DISABLE_PAYLOAD_INDEXES", "0")).strip().lower() in {"1", "true", "yes", "on"},
    )
    parser.add_argument("--qdrant-hnsw-m", type=int, default=int(os.getenv("QDRANT_HNSW_M", "32")))
    parser.add_argument("--qdrant-hnsw-ef-construct", type=int, default=int(os.getenv("QDRANT_HNSW_EF_CONSTRUCT", "128")))
    parser.add_argument(
        "--qdrant-hnsw-on-disk",
        action="store_true",
        default=str(os.getenv("QDRANT_HNSW_ON_DISK", "1")).strip().lower() in {"1", "true", "yes", "on"},
    )
    parser.add_argument(
        "--qdrant-optimizer-indexing-threshold",
        type=int,
        default=int(os.getenv("QDRANT_OPTIMIZER_INDEXING_THRESHOLD", "500")),
    )
    parser.add_argument(
        "--qdrant-optimizer-memmap-threshold",
        type=int,
        default=int(os.getenv("QDRANT_OPTIMIZER_MEMMAP_THRESHOLD", "50000")),
    )
    parser.add_argument("--qdrant-quantization", default=os.getenv("QDRANT_QUANTIZATION", "none"))
    parser.add_argument(
        "--global-collection",
        default=os.getenv("ORCHESTRATOR_GLOBAL_QDRANT_COLLECTION", "orchestrator-global-memory"),
    )
    parser.add_argument(
        "--global-node-types",
        default=os.getenv("ORCHESTRATOR_GLOBAL_NODE_TYPES", GLOBAL_NODE_TYPES_DEFAULT),
    )
    parser.add_argument(
        "--global-max-details-chars",
        type=int,
        default=int(os.getenv("ORCHESTRATOR_GLOBAL_MAX_DETAILS_CHARS", "800")),
    )
    parser.add_argument("--disable-global-sync", action="store_true")
    parser.add_argument("--ollama-url", default=os.getenv("OLLAMA_EMBED_URL", "http://localhost:11434/v1/embeddings"))
    parser.add_argument("--ollama-model", default=os.getenv("OLLAMA_EMBED_MODEL", "all-minilm:latest"))
    parser.add_argument("--ollama-keep-alive", default=os.getenv("OLLAMA_KEEP_ALIVE", "10m"))
    parser.add_argument("--ollama-embed-batch-size", type=int, default=int(os.getenv("OLLAMA_EMBED_BATCH_SIZE", "24")))
    parser.add_argument("--ollama-embed-concurrency", type=int, default=int(os.getenv("OLLAMA_EMBED_CONCURRENCY", "4")))
    parser.add_argument("--ollama-embed-warmup-inputs", type=int, default=int(os.getenv("OLLAMA_EMBED_WARMUP_INPUTS", "32")))
    parser.add_argument("--ollama-embed-max-chars", type=int, default=int(os.getenv("OLLAMA_EMBED_MAX_CHARS", "4000")))
    parser.add_argument(
        "--ollama-embed-batch-size-auto",
        action="store_true",
        default=str(os.getenv("OLLAMA_EMBED_BATCH_SIZE_AUTO", "1")).strip().lower() in {"1", "true", "yes", "on"},
    )
    parser.add_argument("--local-embed-backend", default=os.getenv("LOCAL_EMBED_BACKEND", "hash-projection"))
    parser.add_argument("--neo4j-uri", default=os.getenv("NEO4J_URI", "bolt://localhost:7687"))
    parser.add_argument("--neo4j-user", default=os.getenv("NEO4J_USERNAME", os.getenv("NEO4J_USER", "neo4j")))
    parser.add_argument("--neo4j-password", default=os.getenv("NEO4J_PASSWORD", ""))
    parser.add_argument("--neo4j-database", default=os.getenv("NEO4J_DATABASE", "neo4j"))
    parser.add_argument("--skip-qdrant", action="store_true")
    parser.add_argument("--skip-neo4j", action="store_true")
    return parser.parse_args()


def normalize_relationship_type(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", (value or "").strip()).upper()
    return cleaned or "RELATED_TO"


def normalize_label(value: str) -> str:
    tokens = re.findall(r"[A-Za-z0-9]+", value or "")
    if not tokens:
        return DEFAULT_NODE_LABEL
    label = "".join(token.capitalize() for token in tokens)
    return label if label in ALLOWED_NODE_LABELS else DEFAULT_NODE_LABEL


def normalize_module_name(value: str) -> str:
    cleaned = (value or "").replace("\\", "/").strip("/")
    return cleaned.lower() if cleaned else "root"


def build_module_node_id(module_name: str) -> str:
    return f"module::{normalize_module_name(module_name)}"


def _as_clean_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _normalize_text_list(values):
    if values is None:
        return []
    if isinstance(values, str):
        text = _as_clean_text(values)
        return [text] if text else []
    if not isinstance(values, (list, tuple, set)):
        text = _as_clean_text(values)
        return [text] if text else []
    result = []
    seen = set()
    for item in values:
        text = _as_clean_text(item)
        if not text:
            continue
        if text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _derive_source_modules(source_files):
    modules = []
    seen = set()
    for raw_path in source_files:
        normalized = _as_clean_text(raw_path).replace("\\", "/").strip("/")
        if not normalized:
            continue
        parts = [part for part in normalized.split("/") if part]
        if not parts:
            continue
        path_segments = list(parts)
        if path_segments and "." in path_segments[-1]:
            path_segments = path_segments[:-1]
        if not path_segments:
            continue
        if len(path_segments) >= 2:
            module_name = f"{path_segments[0]}/{path_segments[1]}"
        else:
            module_name = path_segments[0]
        if module_name in seen:
            continue
        seen.add(module_name)
        modules.append(module_name)
    return modules


def compute_node_content_hash(node):
    node_id = _as_clean_text(node.get("id"))
    node_type = _as_clean_text(node.get("node_type")).lower()
    relative_path = _as_clean_text(node.get("relative_path"))
    summary = _as_clean_text(node.get("summary"))
    details = _as_clean_text(node.get("details"))
    tags = "|".join(_normalize_text_list(node.get("tags")))
    source_files = "|".join(_normalize_text_list(node.get("source_files")))
    source_modules = "|".join(_normalize_text_list(node.get("source_modules")))
    basis = "\n".join([node_id, node_type, relative_path, summary, details, tags, source_files, source_modules])
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()


def trim_text_for_embedding(text, max_chars):
    cleaned = _as_clean_text(text)
    if max_chars is None:
        return cleaned
    try:
        cap = int(max_chars)
    except Exception:
        cap = 0
    if cap <= 0 or len(cleaned) <= cap:
        return cleaned
    return cleaned[:cap].rstrip()


def build_qdrant_payload(node, project_slug, embedding_source, content_hash=""):
    relative_path = _as_clean_text(node.get("relative_path"))
    node_id = _as_clean_text(node.get("id")) or relative_path or "unknown-node"
    node_type = _as_clean_text(node.get("node_type")).lower() or "unknown"
    summary = _as_clean_text(node.get("summary")) or f"Memory node: {node_id}"
    details = _as_clean_text(node.get("details")) or f"Source path: {relative_path or 'unknown'}"
    source_files = _normalize_text_list(node.get("source_files"))
    if not source_files and relative_path:
        source_files = [relative_path]
    source_modules = _normalize_text_list(node.get("source_modules"))
    if not source_modules:
        module_name = _as_clean_text(node.get("module_name"))
        if module_name:
            source_modules = [module_name]
        else:
            source_modules = _derive_source_modules(source_files)

    return {
        "project_slug": project_slug,
        "namespace": f"project::{project_slug}",
        "record_scope": "project",
        "record_type": node_type,
        "sensitivity": "internal",
        "node_id": node_id,
        "node_type": node_type,
        "summary": summary,
        "details": details,
        "tags": node.get("tags", []),
        "relative_path": relative_path,
        "module_name": _as_clean_text(node.get("module_name")),
        "source_files": source_files,
        "source_modules": source_modules,
        "embedding_source": embedding_source,
        "content_hash": _as_clean_text(content_hash) or compute_node_content_hash(node),
    }


def parse_csv_set(raw_value: str):
    values = []
    seen = set()
    for item in (raw_value or "").split(","):
        token = _as_clean_text(item).lower()
        if not token:
            continue
        if token in seen:
            continue
        seen.add(token)
        values.append(token)
    return set(values)


def parse_csv_list(raw_value: str):
    values = []
    seen = set()
    for item in (raw_value or "").split(","):
        token = _as_clean_text(item).strip()
        if not token:
            continue
        key = token.lower()
        if key in seen:
            continue
        seen.add(key)
        values.append(token)
    return values


def redact_sensitive_text(text: str):
    cleaned = _as_clean_text(text)
    if not cleaned:
        return ""
    redacted = cleaned
    for pattern in GLOBAL_REDACT_PATTERNS:
        redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def is_sensitive_for_global(payload):
    tags = _normalize_text_list(payload.get("tags", []))
    haystack = " ".join(
        [
            _as_clean_text(payload.get("summary")),
            _as_clean_text(payload.get("details")),
            " ".join(tags),
            _as_clean_text(payload.get("node_type")),
        ]
    ).lower()
    if any(keyword in haystack for keyword in GLOBAL_SENSITIVE_KEYWORDS):
        return True
    for pattern in GLOBAL_REDACT_PATTERNS:
        if pattern.search(haystack):
            return True
    return False


def build_global_qdrant_payload(payload, project_slug, max_details_chars):
    global_payload = dict(payload)
    global_payload["record_scope"] = "global"
    global_payload["source_project_slug"] = project_slug
    global_payload["source_namespace"] = f"project::{project_slug}"
    global_payload["namespace"] = f"project::{project_slug}"
    global_payload["summary"] = redact_sensitive_text(_as_clean_text(payload.get("summary")))
    details = redact_sensitive_text(_as_clean_text(payload.get("details")))
    if max_details_chars > 0 and len(details) > max_details_chars:
        details = details[:max_details_chars].rstrip() + "..."
    global_payload["details"] = details
    global_payload["content_hash"] = hashlib.sha256(
        f"{global_payload.get('summary', '')}\n{global_payload.get('details', '')}".encode("utf-8")
    ).hexdigest()
    return global_payload


def validate_project_slug_or_raise(project_slug: str):
    normalized = (project_slug or "").strip().lower()
    if not normalized:
        raise RuntimeError("project_slug is required and cannot be empty.")
    if not PROJECT_SLUG_PATTERN.match(normalized):
        raise RuntimeError(
            f"project_slug '{project_slug}' is invalid. Expected lowercase slug pattern: {PROJECT_SLUG_PATTERN.pattern}"
        )
    return normalized


def validate_qdrant_payload(payload, expected_project_slug):
    violations = []
    for field in REQUIRED_QDRANT_PAYLOAD_FIELDS:
        value = payload.get(field)
        if value is None:
            violations.append(field)
            continue
        if isinstance(value, str) and not value.strip():
            violations.append(field)
    payload_slug = _as_clean_text(payload.get("project_slug")).lower()
    if payload_slug != (expected_project_slug or "").lower():
        violations.append("project_slug_mismatch")
    return violations


def read_json_file(path: Path):
    if not path or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_env_map(env_path: Path):
    env_map = {}
    if not env_path or not env_path.exists():
        return env_map
    for raw_line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = (raw_line or "").strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key:
            env_map[key] = value.strip()
    return env_map


def looks_like_unusable_secret(value: str):
    secret = (value or "").strip()
    if not secret:
        return True
    if "*" in secret or secret == "[stored in vault]":
        return True
    if secret.startswith("AQAAANCM") and len(secret) >= 128:
        return True
    return False


def resolve_neo4j_password_with_fallback(args, memory_dir: Path, relationships_path: Path):
    if not looks_like_unusable_secret(args.neo4j_password):
        return

    candidate_roots = []
    for base in [Path.cwd(), memory_dir, relationships_path.parent]:
        if not base:
            continue
        try:
            candidate_roots.append(base.resolve())
        except Exception:
            continue

    inspected = set()
    for root in candidate_roots:
        for parent in [root, *root.parents]:
            env_path = parent / "ai-orchestrator" / "docker" / ".env.docker.generated"
            env_key = str(env_path).lower()
            if env_key in inspected:
                continue
            inspected.add(env_key)
            if not env_path.exists():
                continue
            env_map = read_env_map(env_path)
            candidate_password = (env_map.get("NEO4J_PASSWORD") or "").strip()
            if not looks_like_unusable_secret(candidate_password):
                args.neo4j_password = candidate_password
                return


def iter_node_files(memory_dir: Path):
    if not memory_dir.exists():
        return
    for file_path in sorted(memory_dir.rglob("*.md")):
        if file_path.is_file() and file_path.name.lower() != "readme.md":
            yield file_path


def parse_tags(lines):
    tags = []
    for line in lines:
        cleaned = line.strip()
        if not cleaned:
            continue
        if cleaned.startswith("-"):
            cleaned = cleaned[1:].strip()
        if cleaned.startswith("[") and cleaned.endswith("]"):
            cleaned = cleaned[1:-1].strip()
        for part in cleaned.split(","):
            tag = part.strip()
            if tag:
                tags.append(tag)
    return tags


def parse_inline_link(line: str):
    stripped = line.strip()
    if not stripped.startswith("-") or ":" not in stripped:
        return None
    relation_type, remainder = stripped[1:].strip().split(":", 1)
    relation_type = relation_type.strip().lower()
    if relation_type not in INLINE_LINK_TYPES:
        return None
    remainder = remainder.strip()
    if remainder.startswith("[") and "]" in remainder:
        idx = remainder.index("]")
        target = remainder[1:idx].strip()
        reason = remainder[idx + 1 :].strip()
    else:
        parts = re.split(r"\s+[—–-]\s+", remainder, maxsplit=1)
        target = parts[0].strip()
        reason = parts[1].strip() if len(parts) > 1 else ""
    reason = reason.lstrip("—–- ").strip()
    if not target:
        return None
    return {"type": normalize_relationship_type(relation_type), "target": target, "reason": reason}


def parse_node_file(file_path: Path, memory_dir: Path, project_slug: str):
    raw = file_path.read_text(encoding="utf-8", errors="ignore")
    node_id = file_path.stem
    metadata = {}
    sections = {}
    section = None
    for line in raw.splitlines():
        if line.startswith("# Node:"):
            node_id = line.split(":", 1)[1].strip()
            continue
        mm = NODE_METADATA_PATTERN.match(line)
        if mm:
            metadata[mm.group(1).strip().lower()] = mm.group(2).strip()
            section = None
            continue
        sm = SECTION_HEADER_PATTERN.match(line)
        if sm:
            section = sm.group(1).strip().lower()
            sections[section] = []
            continue
        if section:
            sections.setdefault(section, []).append(line)

    relative_path = file_path.relative_to(memory_dir).as_posix()
    module_name = normalize_module_name(relative_path.split("/")[0] if "/" in relative_path else "root")
    inline = []
    for line in sections.get("links", []):
        rel = parse_inline_link(line)
        if rel:
            inline.append({"source": node_id, "type": rel["type"], "target": rel["target"], "reason": rel["reason"], "origin": "node_links"})

    return {
        "id": node_id,
        "node_type": metadata.get("type", "unknown").strip().lower(),
        "label": normalize_label(metadata.get("type", "unknown").strip().lower()),
        "created": metadata.get("created", ""),
        "last_updated": metadata.get("last updated", ""),
        "importance": int(metadata.get("importance", "0")) if str(metadata.get("importance", "0")).isdigit() else 0,
        "summary": "\n".join(sections.get("summary", [])).strip(),
        "details": "\n".join(sections.get("details", [])).strip(),
        "tags": parse_tags(sections.get("tags", [])),
        "project_id": metadata.get("project id", project_slug),
        "project_slug": project_slug,
        "source_path": str(file_path),
        "relative_path": relative_path,
        "module_name": module_name,
        "module_node_id": build_module_node_id(module_name),
        "source_file_node_id": f"file::{relative_path}",
        "raw_content": raw,
        "inline_relationships": inline,
        "placeholder": False,
    }


def parse_relationship_map(relationships_path: Path):
    if not relationships_path.exists():
        return []
    rels = []
    last = None
    in_block = False
    for line in relationships_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_block = not in_block
            continue
        if in_block or not stripped or stripped.startswith("#") or stripped.startswith("|"):
            continue
        if stripped.startswith("Reason:") and last is not None:
            rels[last]["reason"] = stripped.split(":", 1)[1].strip()
            continue
        m = RELATIONSHIP_LINE_PATTERN.match(stripped)
        if not m:
            continue
        src, typ, dst = m.group(1).strip(), normalize_relationship_type(m.group(2)), m.group(3).strip()
        if {src.lower(), dst.lower(), typ.lower()} & {"source_node", "target_node", "relationship_type", "node_id"}:
            continue
        rels.append({"source": src, "type": typ, "target": dst, "reason": "", "origin": "relationship_map"})
        last = len(rels) - 1
    return rels


def parse_intake_dependency_graph(path: Path, project_slug: str):
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        return [], []
    graph = {}
    services = []
    dependencies = []
    if isinstance(payload.get("analysis"), dict) and isinstance(payload["analysis"].get("dependency_graph"), dict):
        graph = payload["analysis"]["dependency_graph"]
    elif isinstance(payload.get("technical_fingerprint"), dict) and isinstance(payload["technical_fingerprint"].get("dependency_graph"), dict):
        graph = payload["technical_fingerprint"]["dependency_graph"]
    elif isinstance(payload.get("dependency_graph"), dict):
        graph = payload["dependency_graph"]
    else:
        # Compatibility with Generate-DependencyGraph.py schema:
        # {"services":[...], "dependencies":[[source,target], ...]}
        if isinstance(payload.get("services"), list):
            services = payload.get("services", []) or []
        if isinstance(payload.get("dependencies"), list):
            dependencies = payload.get("dependencies", []) or []

    modules = set()
    edges = []

    graph_modules = graph.get("modules", []) if isinstance(graph, dict) else []
    graph_edges = graph.get("edges", []) if isinstance(graph, dict) else []
    if not graph_modules and services:
        graph_modules = services
    if not graph_edges and dependencies:
        graph_edges = dependencies

    for mod in graph_modules or []:
        modules.add(normalize_module_name(str(mod)))

    for edge in graph_edges or []:
        source = ""
        target = ""
        if not isinstance(edge, dict):
            if isinstance(edge, (list, tuple)) and len(edge) >= 2:
                source = normalize_module_name(str(edge[0]))
                target = normalize_module_name(str(edge[1]))
            else:
                continue
        else:
            source = normalize_module_name(str(edge.get("source", edge.get("from", ""))))
            target = normalize_module_name(str(edge.get("target", edge.get("to", ""))))
        if not source or not target:
            continue
        modules.add(source)
        modules.add(target)
        reason = "Structural dependency detected by intake analyzer."
        if isinstance(edge, dict):
            reason = str(edge.get("reason", reason))
        else:
            reason = "Structural dependency detected by dependency graph scanner."
        edges.append({"source": build_module_node_id(source), "type": "DEPENDS_ON", "target": build_module_node_id(target), "reason": reason, "origin": "intake_dependency_graph"})

    nodes = []
    for mod in sorted(modules):
        nodes.append({"id": build_module_node_id(mod), "label": "Module", "node_type": "module", "summary": "Technical module identified by intake analyzer.", "details": "Part of the project dependency graph.", "project_id": project_slug, "project_slug": project_slug, "module_name": mod, "tags": ["tech-stack", "module"], "importance": 5, "placeholder": False})

    # NEW: Semantic entities (functions, classes)
    semantic = {}
    if isinstance(payload.get("analysis"), dict) and isinstance(payload["analysis"].get("semantic_graph"), dict):
        semantic = payload["analysis"]["semantic_graph"]
    elif isinstance(payload.get("technical_fingerprint"), dict) and isinstance(payload["technical_fingerprint"].get("dependency_graph"), dict) and isinstance(payload["technical_fingerprint"]["dependency_graph"].get("semantic_graph"), dict):
        semantic = payload["technical_fingerprint"]["dependency_graph"]["semantic_graph"]

    for entity_type in ["functions", "classes"]:
        entities = semantic.get("entities", {}).get(entity_type, [])
        for ent in entities:
            if not isinstance(ent, dict): continue
            name = str(ent.get("name", ""))
            rel_path = str(ent.get("file", ""))
            if not name or not rel_path: continue
            
            node_id = f"{entity_type[:-1]}::{name}" # function::name or class::name
            mod_name = normalize_module_name(rel_path.split("/")[0] if "/" in rel_path else "root")
            nodes.append({
                "id": node_id,
                "label": "Entity",
                "node_type": entity_type[:-1],
                "summary": f"{entity_type[:-1].capitalize()}: {name}",
                "details": f"Located in {rel_path}",
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": ["semantic", entity_type[:-1]],
                "importance": 4,
                "placeholder": False
            })
            edges.append({
                "source": node_id,
                "type": "DEFINED_IN_FILE",
                "target": f"file::{rel_path}",
                "reason": "Entity definition source file.",
                "origin": "semantic_intake_auto"
            })
            edges.append({
                "source": node_id,
                "type": "MEMBER_OF_MODULE",
                "target": build_module_node_id(mod_name),
                "reason": "Entity belongs to module.",
                "origin": "semantic_intake_auto"
            })

    return nodes, edges


def parse_world_model(path: Path, project_slug: str):
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        return [], []
    nodes = []
    rels = []
    for entity in payload.get("entities", []) or []:
        if not isinstance(entity, dict):
            continue
        key = str(entity.get("key", "")).strip().lower()
        if not key:
            continue
        node_id = f"entity::{key}"
        nodes.append({"id": node_id, "label": "Entity", "node_type": "entity", "summary": f"Business entity: {entity.get('name', key)}", "details": f"Attributes: {', '.join(entity.get('attributes', []))}", "project_id": project_slug, "project_slug": project_slug, "tags": ["business-logic", "entity"], "importance": 7, "placeholder": False})
        for f in entity.get("files", []) or []:
            f_norm = str(f).replace("\\", "/").strip("/")
            if not f_norm:
                continue
            mod = normalize_module_name(f_norm.split("/")[0])
            rels.append({"source": node_id, "type": "IMPLEMENTED_IN", "target": build_module_node_id(mod), "reason": f"Entity extracted from `{f_norm}`.", "origin": "world_model_auto"})
        for rel in entity.get("relationships", []) or []:
            if isinstance(rel, dict):
                target_key = str(rel.get("target", "")).strip().lower()
                if target_key:
                    rels.append({"source": node_id, "type": normalize_relationship_type(str(rel.get("type", "RELATES_TO"))), "target": f"entity::{target_key}", "reason": str(rel.get("reason", "")), "origin": "world_model_auto"})
    return nodes, rels


def normalize_repo_path(value: str) -> str:
    text = _as_clean_text(value).replace("\\", "/").strip()
    if text.startswith("./"):
        text = text[2:]
    return text.strip("/")


def infer_node_shape_from_id(node_id: str, project_slug: str) -> dict:
    nid = _as_clean_text(node_id)
    base = {
        "id": nid,
        "label": DEFAULT_NODE_LABEL,
        "node_type": "unknown",
        "summary": "",
        "details": "",
        "project_id": project_slug,
        "project_slug": project_slug,
        "tags": [],
        "importance": 1,
        "placeholder": True,
    }
    if nid.startswith("file::"):
        rel = normalize_repo_path(nid.split("::", 1)[1])
        mod = normalize_module_name(rel.split("/")[0] if rel else "root")
        base.update(
            {
                "label": "File",
                "node_type": "file",
                "summary": f"File node: {rel or nid}",
                "details": f"Derived from relationship endpoint `{nid}`.",
                "relative_path": rel,
                "module_name": mod,
                "module_node_id": build_module_node_id(mod),
                "source_file_node_id": nid,
                "source_files": [rel] if rel else [],
                "source_modules": [mod] if mod else [],
            }
        )
        return base
    if nid.startswith("module::"):
        mod = normalize_module_name(nid.split("::", 1)[1])
        base.update(
            {
                "label": "Module",
                "node_type": "module",
                "summary": f"Module node: {mod}",
                "details": f"Derived from relationship endpoint `{nid}`.",
                "module_name": mod,
                "module_node_id": build_module_node_id(mod),
            }
        )
        return base
    if nid.startswith("task::"):
        base.update(
            {
                "label": "Task",
                "node_type": "task",
                "summary": f"Task node: {nid.split('::', 1)[1]}",
                "details": "Task trace node.",
            }
        )
        return base
    if nid.startswith("agent::"):
        base.update(
            {
                "label": "Agent",
                "node_type": "agent",
                "summary": f"Agent node: {nid.split('::', 1)[1]}",
                "details": "Agent trace node.",
            }
        )
        return base
    if nid.startswith("class:"):
        base.update(
            {
                "label": "Class",
                "node_type": "class",
                "summary": f"Class node: {nid}",
                "details": "AST class relationship endpoint.",
            }
        )
        return base
    if nid.startswith("func:"):
        base.update(
            {
                "label": "Function",
                "node_type": "function",
                "summary": f"Function node: {nid}",
                "details": "AST function relationship endpoint.",
            }
        )
        return base
    if nid.startswith("call:"):
        name = nid.split(":", 1)[1] if ":" in nid else nid
        base.update(
            {
                "label": "Call",
                "node_type": "call",
                "summary": f"Call target: {name}",
                "details": "Symbolic call target inferred from AST.",
                "placeholder": False,
            }
        )
        return base
    return base


def parse_ast_graph(path: Path, project_slug: str):
    payload = read_json_file(path)
    if not isinstance(payload, dict):
        return [], []
    graph = payload.get("graph", {})
    if not isinstance(graph, dict):
        return [], []

    nodes = []
    relationships = []
    known_ids = set()
    raw_nodes = graph.get("nodes", []) or []
    raw_edges = graph.get("edges", []) or []

    for raw in raw_nodes:
        if not isinstance(raw, dict):
            continue
        node_id = _as_clean_text(raw.get("id"))
        if not node_id:
            continue
        rel_path = normalize_repo_path(_as_clean_text(raw.get("file")))
        module_name = normalize_module_name(rel_path.split("/")[0] if rel_path else "root")
        node_type = _as_clean_text(raw.get("type")).lower() or "code"
        label_map = {
            "class": "Class",
            "function": "Function",
            "module": "Module",
            "call": "Call",
        }
        label = label_map.get(node_type, "Code")
        summary_name = _as_clean_text(raw.get("name")) or node_id
        line_number = int(raw.get("line", 0) or 0)
        details = f"AST source: {rel_path}:{line_number}" if rel_path else "AST source node."
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "node_type": node_type,
                "summary": f"{node_type.capitalize()}: {summary_name}",
                "details": details,
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": ["ast", node_type],
                "importance": 4,
                "placeholder": False,
                "relative_path": rel_path,
                "source_path": rel_path,
                "module_name": module_name if rel_path else "",
                "module_node_id": build_module_node_id(module_name) if rel_path else "",
                "source_file_node_id": f"file::{rel_path}" if rel_path else "",
                "source_files": [rel_path] if rel_path else [],
                "source_modules": [module_name] if rel_path else [],
                "created": "",
                "last_updated": "",
            }
        )
        known_ids.add(node_id)

    for edge in raw_edges:
        if not isinstance(edge, dict):
            continue
        source = _as_clean_text(edge.get("from"))
        target = _as_clean_text(edge.get("to"))
        if not source or not target:
            continue
        rel_type = normalize_relationship_type(_as_clean_text(edge.get("kind")) or "RELATED_TO")
        relationships.append(
            {
                "source": source,
                "type": rel_type,
                "target": target,
                "reason": f"AST relation: {rel_type.lower()}",
                "origin": "ast_graph",
            }
        )

        if source.startswith("call:") and source not in known_ids:
            nodes.append(infer_node_shape_from_id(source, project_slug))
            known_ids.add(source)
        if target.startswith("call:") and target not in known_ids:
            nodes.append(infer_node_shape_from_id(target, project_slug))
            known_ids.add(target)

    return nodes, relationships


def sanitize_agent_id(value: str) -> str:
    raw = _as_clean_text(value).lower()
    raw = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-")
    return raw or "unknown-agent"


def parse_task_trace(task_dag_path: Path, completions_dir: Path, project_slug: str):
    tasks_index = {}

    if task_dag_path and task_dag_path.exists():
        dag_payload = read_json_file(task_dag_path)
        if isinstance(dag_payload, dict):
            tasks = dag_payload.get("tasks", []) or []
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                task_id = _as_clean_text(task.get("id"))
                if not task_id:
                    continue
                tasks_index[task_id] = {
                    "task": task,
                    "agents": set(),
                    "changed_files": set(),
                }
                assigned = _as_clean_text(task.get("assigned_agent"))
                if assigned:
                    tasks_index[task_id]["agents"].add(assigned)
                for field_name in ("files_affected", "artifacts", "source_files"):
                    for raw_path in task.get(field_name, []) or []:
                        norm = normalize_repo_path(raw_path)
                        if norm:
                            tasks_index[task_id]["changed_files"].add(norm)

    if completions_dir and completions_dir.exists():
        for completion_file in sorted(completions_dir.glob("*.json")):
            payload = read_json_file(completion_file)
            if not isinstance(payload, dict):
                continue
            task_id = _as_clean_text(payload.get("task_id"))
            if not task_id:
                continue
            if task_id not in tasks_index:
                tasks_index[task_id] = {"task": {"id": task_id}, "agents": set(), "changed_files": set()}
            agent_name = _as_clean_text(payload.get("agent_name"))
            if agent_name:
                tasks_index[task_id]["agents"].add(agent_name)
            for field_name in ("files_written", "changes", "source_files", "artifacts"):
                for raw_path in payload.get(field_name, []) or []:
                    if not isinstance(raw_path, str):
                        continue
                    norm = normalize_repo_path(raw_path)
                    if norm and not norm.endswith(".json"):
                        tasks_index[task_id]["changed_files"].add(norm)

    nodes = []
    relationships = []
    for task_id, bundle in tasks_index.items():
        task = bundle.get("task", {}) or {}
        agents = sorted(bundle.get("agents", set()))
        changed_files = sorted(bundle.get("changed_files", set()))
        task_node_id = f"task::{task_id}"
        status = _as_clean_text(task.get("status")) or "unknown"
        title = _as_clean_text(task.get("title")) or _as_clean_text(task.get("description")) or task_id
        description = _as_clean_text(task.get("description"))
        source_modules = _derive_source_modules(changed_files)
        nodes.append(
            {
                "id": task_node_id,
                "label": "Task",
                "node_type": "task",
                "summary": title,
                "details": description,
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": ["task-trace", f"status:{status.lower()}"],
                "importance": 5,
                "placeholder": False,
                "created": _as_clean_text(task.get("created_at")),
                "last_updated": _as_clean_text(task.get("updated_at")) or _as_clean_text(task.get("completed_at")),
                "source_files": changed_files,
                "source_modules": source_modules,
            }
        )

        for dep in task.get("dependencies", []) or []:
            dep_id = _as_clean_text(dep)
            if not dep_id:
                continue
            relationships.append(
                {
                    "source": task_node_id,
                    "type": "TASK_DEPENDS_ON",
                    "target": f"task::{dep_id}",
                    "reason": "Task dependency from DAG.",
                    "origin": "task_trace",
                }
            )

        for agent_name in agents:
            agent_node_id = f"agent::{sanitize_agent_id(agent_name)}"
            nodes.append(
                {
                    "id": agent_node_id,
                    "label": "Agent",
                    "node_type": "agent",
                    "summary": f"Agent: {agent_name}",
                    "details": "Execution actor from task DAG/completions.",
                    "project_id": project_slug,
                    "project_slug": project_slug,
                    "tags": ["task-trace", "agent"],
                    "importance": 3,
                    "placeholder": False,
                }
            )
            relationships.append(
                {
                    "source": task_node_id,
                    "type": "CREATED_BY",
                    "target": agent_node_id,
                    "reason": "Task ownership/execution attribution.",
                    "origin": "task_trace",
                }
            )

        for rel_path in changed_files:
            file_node_id = f"file::{rel_path}"
            module_name = normalize_module_name(rel_path.split("/")[0] if rel_path else "root")
            nodes.append(
                {
                    "id": file_node_id,
                    "label": "File",
                    "node_type": "file",
                    "summary": f"File touched by {task_id}",
                    "details": f"Changed by task `{task_id}`.",
                    "project_id": project_slug,
                    "project_slug": project_slug,
                    "tags": ["task-trace", "code-change"],
                    "importance": 4,
                    "placeholder": False,
                    "relative_path": rel_path,
                    "module_name": module_name,
                    "module_node_id": build_module_node_id(module_name),
                    "source_file_node_id": file_node_id,
                    "source_files": [rel_path],
                    "source_modules": [module_name] if module_name else [],
                }
            )
            relationships.append(
                {
                    "source": task_node_id,
                    "type": "MODIFIED",
                    "target": file_node_id,
                    "reason": "File modified by task completion.",
                    "origin": "task_trace",
                }
            )

    return nodes, relationships


def infer_project_root(
    project_root_arg: str,
    memory_dir: Path,
    dep_path: Path | None,
    task_dag_path: Path | None,
) -> Path | None:
    if project_root_arg:
        candidate = Path(project_root_arg).resolve()
        return candidate if candidate.exists() else None

    candidates = []
    if dep_path is not None:
        candidates.append(dep_path.parent.parent)
    if task_dag_path is not None:
        candidates.append(task_dag_path.parent.parent.parent)
    if memory_dir:
        if memory_dir.name == "nodes" and memory_dir.parent.name == "memory_graph":
            candidates.append(memory_dir.parent.parent)
        if memory_dir.name == "memory" and memory_dir.parent.parent.name == "projects":
            candidates.append(memory_dir.parent.parent.parent.parent)

    for candidate in candidates:
        if candidate and candidate.exists() and (candidate / "routes").exists():
            return candidate.resolve()
    return None


def stable_short_hash(text: str) -> str:
    return hashlib.sha1(_as_clean_text(text).encode("utf-8")).hexdigest()[:12]


def infer_controller_file_path(controller_fqn: str) -> str:
    fqn = _as_clean_text(controller_fqn).strip("\\")
    if not fqn:
        return ""
    if fqn.startswith("App\\"):
        rel = "app/" + fqn[4:].replace("\\", "/") + ".php"
    else:
        rel = fqn.replace("\\", "/") + ".php"
    return normalize_repo_path(rel)


def route_node_id(http_method: str, uri: str) -> str:
    method = (_as_clean_text(http_method) or "ANY").upper()
    normalized_uri = "/" + normalize_repo_path(uri).lstrip("/")
    return f"route::{method}::{normalized_uri}"


def parse_laravel_route_graph(project_root: Path, project_slug: str):
    routes_dir = project_root / "routes"
    if not routes_dir.exists():
        return [], []

    nodes = []
    relationships = []
    http_actions_pattern = re.compile(
        r"Route::(get|post|put|patch|delete|options|any|match)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*(.+?)\)\s*;",
        re.IGNORECASE | re.DOTALL,
    )
    resource_pattern = re.compile(
        r"Route::(?:apiResource|resource)\s*\(\s*['\"]([^'\"]+)['\"]\s*,\s*([A-Za-z0-9_\\]+)::class",
        re.IGNORECASE,
    )
    use_pattern = re.compile(r"^\s*use\s+([A-Za-z0-9_\\]+)\s*;", re.MULTILINE)
    array_controller_pattern = re.compile(r"\[\s*([A-Za-z0-9_\\]+)::class\s*,\s*['\"]([A-Za-z0-9_]+)['\"]\s*\]")
    at_controller_pattern = re.compile(r"['\"]([A-Za-z0-9_\\]+)@([A-Za-z0-9_]+)['\"]")

    for route_file in sorted(routes_dir.glob("*.php")):
        rel_path = normalize_repo_path(route_file.relative_to(project_root).as_posix())
        file_node_id = f"file::{rel_path}"
        nodes.append(
            {
                "id": file_node_id,
                "label": "File",
                "node_type": "file",
                "summary": f"Route file: {rel_path}",
                "details": "Laravel route declaration file.",
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": ["route-graph", "laravel"],
                "importance": 3,
                "placeholder": False,
                "relative_path": rel_path,
                "module_name": normalize_module_name("routes"),
                "module_node_id": build_module_node_id("routes"),
                "source_file_node_id": file_node_id,
                "source_files": [rel_path],
                "source_modules": ["routes"],
            }
        )
        content = route_file.read_text(encoding="utf-8", errors="ignore")
        import_map = {}
        for used in use_pattern.findall(content):
            class_name = _as_clean_text(used.split("\\")[-1])
            if class_name:
                import_map[class_name] = _as_clean_text(used)

        for m in http_actions_pattern.finditer(content):
            method = m.group(1).upper()
            uri = _as_clean_text(m.group(2))
            handler_blob = _as_clean_text(m.group(3))
            rid = route_node_id(method, uri)
            nodes.append(
                {
                    "id": rid,
                    "label": "Route",
                    "node_type": "route",
                    "summary": f"{method} {uri}",
                    "details": f"Declared in {rel_path}",
                    "project_id": project_slug,
                    "project_slug": project_slug,
                    "tags": ["route-graph", "http-route"],
                    "importance": 5,
                    "placeholder": False,
                    "relative_path": rel_path,
                    "source_files": [rel_path],
                    "source_modules": ["routes"],
                }
            )
            relationships.append(
                {
                    "source": rid,
                    "type": "DECLARED_IN",
                    "target": file_node_id,
                    "reason": "Route declared in routes file.",
                    "origin": "route_graph",
                }
            )

            controller_fqn = ""
            controller_method = ""
            mc = array_controller_pattern.search(handler_blob)
            if mc:
                controller_fqn = _as_clean_text(mc.group(1))
                controller_method = _as_clean_text(mc.group(2))
            else:
                ma = at_controller_pattern.search(handler_blob)
                if ma:
                    raw_controller = _as_clean_text(ma.group(1))
                    controller_method = _as_clean_text(ma.group(2))
                    controller_fqn = import_map.get(raw_controller, raw_controller)

            if controller_fqn:
                if "\\" not in controller_fqn and controller_fqn in import_map:
                    controller_fqn = import_map[controller_fqn]
                class_ref_id = f"classref::{controller_fqn}"
                method_ref_id = f"method::{controller_fqn}::{controller_method}" if controller_method else ""
                controller_rel_path = infer_controller_file_path(controller_fqn)

                nodes.append(
                    {
                        "id": class_ref_id,
                        "label": "Class",
                        "node_type": "class",
                        "summary": f"Controller class: {controller_fqn}",
                        "details": "Resolved from Laravel route declaration.",
                        "project_id": project_slug,
                        "project_slug": project_slug,
                        "tags": ["route-graph", "controller"],
                        "importance": 4,
                        "placeholder": False,
                    }
                )
                relationships.append(
                    {
                        "source": rid,
                        "type": "HANDLED_BY",
                        "target": class_ref_id,
                        "reason": "Route handler controller class.",
                        "origin": "route_graph",
                    }
                )
                if method_ref_id:
                    nodes.append(
                        {
                            "id": method_ref_id,
                            "label": "Function",
                            "node_type": "function",
                            "summary": f"Controller method: {controller_method}",
                            "details": f"Handler method for route {method} {uri}",
                            "project_id": project_slug,
                            "project_slug": project_slug,
                            "tags": ["route-graph", "controller-method"],
                            "importance": 4,
                            "placeholder": False,
                        }
                    )
                    relationships.append(
                        {
                            "source": rid,
                            "type": "INVOKES",
                            "target": method_ref_id,
                            "reason": "Route invokes controller method.",
                            "origin": "route_graph",
                        }
                    )
                    relationships.append(
                        {
                            "source": class_ref_id,
                            "type": "CONTAINS",
                            "target": method_ref_id,
                            "reason": "Method belongs to controller class.",
                            "origin": "route_graph",
                        }
                    )
                if controller_rel_path:
                    controller_file_node = f"file::{controller_rel_path}"
                    mod_name = normalize_module_name(controller_rel_path.split("/")[0])
                    nodes.append(
                        {
                            "id": controller_file_node,
                            "label": "File",
                            "node_type": "file",
                            "summary": f"Controller file: {controller_rel_path}",
                            "details": "Resolved from controller class FQN.",
                            "project_id": project_slug,
                            "project_slug": project_slug,
                            "tags": ["route-graph", "controller-file"],
                            "importance": 3,
                            "placeholder": False,
                            "relative_path": controller_rel_path,
                            "module_name": mod_name,
                            "module_node_id": build_module_node_id(mod_name),
                            "source_file_node_id": controller_file_node,
                            "source_files": [controller_rel_path],
                            "source_modules": [mod_name],
                        }
                    )
                    relationships.append(
                        {
                            "source": class_ref_id,
                            "type": "DEFINED_IN_FILE",
                            "target": controller_file_node,
                            "reason": "Controller class file mapping.",
                            "origin": "route_graph",
                        }
                    )

        for m in resource_pattern.finditer(content):
            resource_uri = _as_clean_text(m.group(1))
            controller_fqn = _as_clean_text(m.group(2))
            for verb in ("GET", "POST", "PUT", "PATCH", "DELETE"):
                rid = route_node_id(verb, resource_uri)
                nodes.append(
                    {
                        "id": rid,
                        "label": "Route",
                        "node_type": "route",
                        "summary": f"{verb} {resource_uri}",
                        "details": f"Resource route generated from {controller_fqn}.",
                        "project_id": project_slug,
                        "project_slug": project_slug,
                        "tags": ["route-graph", "resource-route"],
                        "importance": 4,
                        "placeholder": False,
                    }
                )
                relationships.append(
                    {
                        "source": rid,
                        "type": "HANDLED_BY",
                        "target": f"classref::{controller_fqn}",
                        "reason": "Resource route mapped to controller.",
                        "origin": "route_graph",
                    }
                )

    return nodes, relationships


def parse_database_schema_graph(project_root: Path, project_slug: str):
    migrations_dir = project_root / "database" / "migrations"
    if not migrations_dir.exists():
        return [], []

    nodes = []
    relationships = []
    block_pattern = re.compile(
        r"Schema::(?:create|table)\(\s*['\"]([^'\"]+)['\"]\s*,\s*function\s*\([^\)]*\)\s*\{(?P<body>.*?)\}\s*\)\s*;",
        re.DOTALL | re.IGNORECASE,
    )
    column_pattern = re.compile(r"\$table->([A-Za-z_][A-Za-z0-9_]*)\(\s*['\"]([^'\"]+)['\"]")
    index_pattern = re.compile(r"\$table->(index|unique|primary|fullText|spatialIndex)\(\s*([^\)]*)\)", re.IGNORECASE)
    foreign_pattern = re.compile(
        r"\$table->foreign\(\s*['\"]([^'\"]+)['\"]\s*\)\s*->references\(\s*['\"]([^'\"]+)['\"]\s*\)\s*->on\(\s*['\"]([^'\"]+)['\"]\s*\)",
        re.IGNORECASE,
    )
    constrained_pattern = re.compile(
        r"\$table->foreignId\(\s*['\"]([^'\"]+)['\"]\s*\)(?:->constrained\(\s*['\"]([^'\"]+)['\"]\s*\)|->constrained\(\s*\))",
        re.IGNORECASE,
    )
    column_methods = {
        "string",
        "text",
        "longtext",
        "integer",
        "biginteger",
        "boolean",
        "date",
        "datetime",
        "timestamp",
        "timestamps",
        "json",
        "decimal",
        "double",
        "float",
        "uuid",
        "char",
        "foreignid",
        "unsignedbiginteger",
        "unsignedinteger",
    }
    pii_keywords = {kw.lower() for kw in GLOBAL_SENSITIVE_KEYWORDS}

    for migration_file in sorted(migrations_dir.glob("*.php")):
        rel_path = normalize_repo_path(migration_file.relative_to(project_root).as_posix())
        migration_id = f"migration::{stable_short_hash(rel_path)}"
        nodes.append(
            {
                "id": migration_id,
                "label": "Migration",
                "node_type": "migration",
                "summary": f"Migration: {rel_path}",
                "details": "Database migration artifact.",
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": ["db-schema", "migration"],
                "importance": 4,
                "placeholder": False,
                "relative_path": rel_path,
                "source_files": [rel_path],
                "source_modules": ["database/migrations"],
            }
        )
        content = migration_file.read_text(encoding="utf-8", errors="ignore")

        for mb in block_pattern.finditer(content):
            table_name = normalize_module_name(_as_clean_text(mb.group(1)))
            body = mb.group("body")
            if not table_name:
                continue
            table_id = f"table::{table_name}"
            nodes.append(
                {
                    "id": table_id,
                    "label": "Table",
                    "node_type": "table",
                    "summary": f"Table: {table_name}",
                    "details": f"Discovered in migration {rel_path}",
                    "project_id": project_slug,
                    "project_slug": project_slug,
                    "tags": ["db-schema", "table"],
                    "importance": 5,
                    "placeholder": False,
                }
            )
            relationships.append(
                {
                    "source": migration_id,
                    "type": "MIGRATES",
                    "target": table_id,
                    "reason": "Migration touches table.",
                    "origin": "schema_graph",
                }
            )

            for cm in column_pattern.finditer(body):
                col_method = _as_clean_text(cm.group(1)).lower()
                col_name = normalize_module_name(_as_clean_text(cm.group(2)))
                if not col_name or col_method not in column_methods:
                    continue
                col_id = f"column::{table_name}::{col_name}"
                tags = ["db-schema", "column", f"type:{col_method}"]
                if any(key in col_name.lower() for key in pii_keywords):
                    tags.append("pii")
                nodes.append(
                    {
                        "id": col_id,
                        "label": "Column",
                        "node_type": "column",
                        "summary": f"{table_name}.{col_name}",
                        "details": f"Column method `{col_method}` in migration {rel_path}.",
                        "project_id": project_slug,
                        "project_slug": project_slug,
                        "tags": tags,
                        "importance": 4,
                        "placeholder": False,
                    }
                )
                relationships.append(
                    {
                        "source": table_id,
                        "type": "HAS_COLUMN",
                        "target": col_id,
                        "reason": "Column declared in table blueprint.",
                        "origin": "schema_graph",
                    }
                )
                if "pii" in tags:
                    risk_id = "risk::pii"
                    nodes.append(
                        {
                            "id": risk_id,
                            "label": "Risk",
                            "node_type": "risk",
                            "summary": "PII/PHI data handling risk",
                            "details": "Sensitive field identified by schema scanner.",
                            "project_id": project_slug,
                            "project_slug": project_slug,
                            "tags": ["security", "lgpd", "risk"],
                            "importance": 6,
                            "placeholder": False,
                        }
                    )
                    relationships.append(
                        {
                            "source": col_id,
                            "type": "HAS_RISK",
                            "target": risk_id,
                            "reason": "Column name matches sensitive data keyword.",
                            "origin": "schema_graph",
                        }
                    )

            for im in index_pattern.finditer(body):
                index_kind = _as_clean_text(im.group(1)).lower()
                raw_cols = _as_clean_text(im.group(2))
                index_id = f"index::{table_name}::{stable_short_hash(index_kind + '|' + raw_cols)}"
                nodes.append(
                    {
                        "id": index_id,
                        "label": "Index",
                        "node_type": "index",
                        "summary": f"{index_kind} index on {table_name}",
                        "details": raw_cols,
                        "project_id": project_slug,
                        "project_slug": project_slug,
                        "tags": ["db-schema", "index", index_kind],
                        "importance": 3,
                        "placeholder": False,
                    }
                )
                relationships.append(
                    {
                        "source": table_id,
                        "type": "HAS_INDEX",
                        "target": index_id,
                        "reason": "Index declared in migration.",
                        "origin": "schema_graph",
                    }
                )

            for fm in foreign_pattern.finditer(body):
                source_col = normalize_module_name(_as_clean_text(fm.group(1)))
                target_table = normalize_module_name(_as_clean_text(fm.group(3)))
                if not source_col or not target_table:
                    continue
                relationships.append(
                    {
                        "source": f"column::{table_name}::{source_col}",
                        "type": "REFERENCES",
                        "target": f"table::{target_table}",
                        "reason": "Explicit foreign key reference.",
                        "origin": "schema_graph",
                    }
                )
                relationships.append(
                    {
                        "source": f"table::{table_name}",
                        "type": "DEPENDS_ON",
                        "target": f"table::{target_table}",
                        "reason": "Foreign key table dependency.",
                        "origin": "schema_graph",
                    }
                )

            for cm in constrained_pattern.finditer(body):
                source_col = normalize_module_name(_as_clean_text(cm.group(1)))
                explicit_target = normalize_module_name(_as_clean_text(cm.group(2)))
                if not source_col:
                    continue
                target_table = explicit_target
                if not target_table:
                    target_table = source_col
                    if target_table.endswith("_id"):
                        target_table = target_table[:-3]
                    if not target_table.endswith("s"):
                        target_table += "s"
                relationships.append(
                    {
                        "source": f"column::{table_name}::{source_col}",
                        "type": "REFERENCES",
                        "target": f"table::{target_table}",
                        "reason": "foreignId constrained relation.",
                        "origin": "schema_graph",
                    }
                )
                relationships.append(
                    {
                        "source": f"table::{table_name}",
                        "type": "DEPENDS_ON",
                        "target": f"table::{target_table}",
                        "reason": "foreignId constrained table dependency.",
                        "origin": "schema_graph",
                    }
                )

    return nodes, relationships


def parse_test_observability_graph(project_root: Path, project_slug: str):
    tests_root = project_root / "tests"
    if not tests_root.exists():
        return [], []

    nodes = []
    relationships = []
    method_pattern = re.compile(r"function\s+(test_[A-Za-z0-9_]+)\s*\(", re.IGNORECASE)
    it_pattern = re.compile(r"\bit\s*\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    route_call_pattern = re.compile(r"->(get|post|put|patch|delete|options)\(\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    json_call_pattern = re.compile(r"->json\(\s*['\"]([A-Za-z]+)['\"]\s*,\s*['\"]([^'\"]+)['\"]", re.IGNORECASE)
    class_use_pattern = re.compile(r"^\s*use\s+([A-Za-z0-9_\\]+)\s*;", re.MULTILINE)

    for test_file in sorted(tests_root.rglob("*.php")):
        rel_path = normalize_repo_path(test_file.relative_to(project_root).as_posix())
        content = test_file.read_text(encoding="utf-8", errors="ignore")
        test_names = []
        test_names.extend([_as_clean_text(v) for v in method_pattern.findall(content)])
        test_names.extend([f"it_{stable_short_hash(_as_clean_text(v))}" for v in it_pattern.findall(content)])
        if not test_names:
            continue

        file_node_id = f"file::{rel_path}"
        nodes.append(
            {
                "id": file_node_id,
                "label": "File",
                "node_type": "file",
                "summary": f"Test file: {rel_path}",
                "details": "Automated test source.",
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": ["test-graph", "test-file"],
                "importance": 3,
                "placeholder": False,
                "relative_path": rel_path,
                "source_files": [rel_path],
                "source_modules": _derive_source_modules([rel_path]),
            }
        )

        controller_refs = []
        for used in class_use_pattern.findall(content):
            if _as_clean_text(used).startswith("App\\Http\\Controllers\\"):
                controller_refs.append(_as_clean_text(used))

        for idx, test_name in enumerate(sorted(set(test_names))):
            test_node_id = f"test::{rel_path}::{test_name}:{idx+1}"
            nodes.append(
                {
                    "id": test_node_id,
                    "label": "Test",
                    "node_type": "test",
                    "summary": f"Test: {test_name}",
                    "details": f"Defined in {rel_path}",
                    "project_id": project_slug,
                    "project_slug": project_slug,
                    "tags": ["test-graph", "automated-test"],
                    "importance": 4,
                    "placeholder": False,
                }
            )
            relationships.append(
                {
                    "source": test_node_id,
                    "type": "DEFINED_IN_FILE",
                    "target": file_node_id,
                    "reason": "Test declared in file.",
                    "origin": "test_graph",
                }
            )

            for rc in route_call_pattern.findall(content):
                method = _as_clean_text(rc[0]).upper()
                uri = _as_clean_text(rc[1])
                rid = route_node_id(method, uri)
                nodes.append(
                    {
                        "id": rid,
                        "label": "Route",
                        "node_type": "route",
                        "summary": f"{method} {uri}",
                        "details": "Route referenced by test HTTP call.",
                        "project_id": project_slug,
                        "project_slug": project_slug,
                        "tags": ["test-graph", "route-reference"],
                        "importance": 3,
                        "placeholder": False,
                    }
                )
                relationships.append(
                    {
                        "source": test_node_id,
                        "type": "COVERS",
                        "target": rid,
                        "reason": "Test exercises HTTP endpoint.",
                        "origin": "test_graph",
                    }
                )

            for jc in json_call_pattern.findall(content):
                method = _as_clean_text(jc[0]).upper()
                uri = _as_clean_text(jc[1])
                rid = route_node_id(method, uri)
                nodes.append(
                    {
                        "id": rid,
                        "label": "Route",
                        "node_type": "route",
                        "summary": f"{method} {uri}",
                        "details": "Route referenced by JSON request in test.",
                        "project_id": project_slug,
                        "project_slug": project_slug,
                        "tags": ["test-graph", "route-reference"],
                        "importance": 3,
                        "placeholder": False,
                    }
                )
                relationships.append(
                    {
                        "source": test_node_id,
                        "type": "COVERS",
                        "target": rid,
                        "reason": "Test exercises JSON endpoint.",
                        "origin": "test_graph",
                    }
                )

            for controller_fqn in controller_refs:
                class_ref_id = f"classref::{controller_fqn}"
                relationships.append(
                    {
                        "source": test_node_id,
                        "type": "VALIDATES",
                        "target": class_ref_id,
                        "reason": "Test imports controller class.",
                        "origin": "test_graph",
                    }
                )

    return nodes, relationships


def parse_incident_graph(project_root: Path, project_slug: str):
    reports_dir = project_root / "ai-orchestrator" / "reports"
    if not reports_dir.exists():
        return [], []

    nodes = []
    relationships = []
    task_id_pattern = re.compile(r"\b([A-Z]{3,}(?:-[A-Z0-9]+){1,})\b")

    for incident in sorted(reports_dir.glob("INCIDENT_*.md")):
        rel_path = normalize_repo_path(incident.relative_to(project_root).as_posix())
        stem = incident.stem
        incident_id = f"incident::{stem}"
        content = incident.read_text(encoding="utf-8", errors="ignore")
        first_line = ""
        for line in content.splitlines():
            if line.strip():
                first_line = line.strip().lstrip("#").strip()
                break
        nodes.append(
            {
                "id": incident_id,
                "label": "Incident",
                "node_type": "incident",
                "summary": first_line or stem,
                "details": f"Incident report `{rel_path}`",
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": ["incident", "ops"],
                "importance": 5,
                "placeholder": False,
                "relative_path": rel_path,
                "source_files": [rel_path],
                "source_modules": ["ai-orchestrator/reports"],
            }
        )
        for task_id in sorted(set(task_id_pattern.findall(content))):
            if len(task_id) < 8:
                continue
            relationships.append(
                {
                    "source": incident_id,
                    "type": "RELATED_TO_TASK",
                    "target": f"task::{task_id}",
                    "reason": "Task identifier found in incident report.",
                    "origin": "incident_graph",
                }
            )

    return nodes, relationships


def parse_roadmap_business_graph(project_root: Path, project_slug: str):
    roadmap_candidates = [
        project_root / "ai-orchestrator" / "memory" / "roadmap.md",
        project_root / "ai-orchestrator" / "roadmap.md",
        project_root / "roadmap.md",
    ]
    roadmap_path = next((path for path in roadmap_candidates if path.exists()), None)
    if roadmap_path is None:
        return [], []

    nodes = []
    relationships = []
    task_id_pattern = re.compile(r"\b([A-Z]{3,}(?:-[A-Z0-9]+){1,})\b")
    checklist_pattern = re.compile(r"^\s*-\s*\[( |x|X)\]\s+(.+)$")
    bullet_pattern = re.compile(r"^\s*-\s+(.+)$")
    heading_pattern = re.compile(r"^\s*#{2,6}\s+(.+)$")

    section_status_map = {
        "current": "status:pending",
        "next": "status:pending",
        "backlog": "status:pending",
        "in progress": "status:in-progress",
        "done": "status:done",
        "completed": "status:done",
    }
    current_section_status = "status:pending"

    content = roadmap_path.read_text(encoding="utf-8", errors="ignore")
    for raw_line in content.splitlines():
        heading_match = heading_pattern.match(raw_line)
        if heading_match:
            heading_text = _as_clean_text(heading_match.group(1)).lower()
            current_section_status = section_status_map.get(heading_text, current_section_status)
            continue

        checked = False
        description = ""

        checklist_match = checklist_pattern.match(raw_line)
        if checklist_match:
            checked = checklist_match.group(1).strip().lower() == "x"
            description = _as_clean_text(checklist_match.group(2))
        else:
            bullet_match = bullet_pattern.match(raw_line)
            if not bullet_match:
                continue
            description = _as_clean_text(bullet_match.group(1))

        if not description:
            continue

        requirement_id = f"requirement::{stable_short_hash(description)}"
        status_tag = "status:done" if checked else current_section_status
        tags = ["business-rule", "roadmap", status_tag]
        nodes.append(
            {
                "id": requirement_id,
                "label": "Requirement",
                "node_type": "requirement",
                "summary": description,
                "details": "Requirement extracted from roadmap.md.",
                "project_id": project_slug,
                "project_slug": project_slug,
                "tags": tags,
                "importance": 6,
                "placeholder": False,
                "source_files": [normalize_repo_path(roadmap_path.relative_to(project_root).as_posix())],
                "source_modules": [normalize_repo_path(str(roadmap_path.parent.relative_to(project_root))).replace("\\", "/")],
            }
        )
        for task_id in sorted(set(task_id_pattern.findall(description))):
            if len(task_id) < 8:
                continue
            relationships.append(
                {
                    "source": requirement_id,
                    "type": "TRACKED_BY",
                    "target": f"task::{task_id}",
                    "reason": "Roadmap line references task identifier.",
                    "origin": "roadmap_graph",
                }
            )

    return nodes, relationships


def dedupe_nodes(nodes):
    out = {}

    def _merge_list(a, b):
        merged = []
        seen = set()
        for value in list(a or []) + list(b or []):
            text = _as_clean_text(value)
            if not text or text in seen:
                continue
            seen.add(text)
            merged.append(text)
        return merged

    for node in nodes:
        nid = node.get("id")
        if not nid:
            continue
        prev = out.get(nid)
        if not prev:
            out[nid] = node
            continue
        merged = dict(prev)
        prev_score = len(_as_clean_text(prev.get("summary"))) + len(_as_clean_text(prev.get("details")))
        now_score = len(_as_clean_text(node.get("summary"))) + len(_as_clean_text(node.get("details")))
        if now_score > prev_score:
            merged.update(node)
        else:
            for key, value in node.items():
                if key not in merged or merged.get(key) in ("", None, [], {}):
                    merged[key] = value

        merged["tags"] = _merge_list(prev.get("tags"), node.get("tags"))
        merged["source_files"] = _merge_list(prev.get("source_files"), node.get("source_files"))
        merged["source_modules"] = _merge_list(prev.get("source_modules"), node.get("source_modules"))
        if prev.get("placeholder") is False or node.get("placeholder") is False:
            merged["placeholder"] = False
        out[nid] = merged
    return list(out.values())


def collect_relationships(memory_nodes, relationships_path, extra_relationships):
    dedup = {}

    def remember(rel):
        key = (rel["source"], rel["type"], rel["target"])
        if key not in dedup or (not dedup[key].get("reason") and rel.get("reason")):
            dedup[key] = rel

    for node in memory_nodes:
        for rel in node.get("inline_relationships", []):
            remember(rel)
        if node.get("source_file_node_id") and node.get("module_node_id"):
            remember({"source": node["source_file_node_id"], "type": "IMPLEMENTS", "target": node["id"], "reason": "Markdown file implements the logical memory node.", "origin": "document_sync"})
            remember({"source": node["source_file_node_id"], "type": "DEPENDS_ON", "target": node["module_node_id"], "reason": "Markdown file belongs to the module directory.", "origin": "document_sync"})

    for rel in parse_relationship_map(relationships_path):
        remember(rel)
    for rel in extra_relationships:
        remember(rel)
    return list(dedup.values())


def ensure_collection(client, collection_name, vector_size):
    existing = {c.name for c in client.get_collections().collections}
    if collection_name not in existing:
        client.create_collection(collection_name=collection_name, vectors_config=qdrant_models.VectorParams(size=vector_size, distance=qdrant_models.Distance.COSINE))
    return vector_size


def apply_collection_tuning(client, collection_name, args):
    if qdrant_models is None:
        return {"enabled": False, "reason": "qdrant-models-unavailable", "applied": False, "errors": []}

    errors = []
    update_kwargs = {}
    applied_features = []

    try:
        if hasattr(qdrant_models, "HnswConfigDiff"):
            hnsw_kwargs = {
                "m": max(int(getattr(args, "qdrant_hnsw_m", 32) or 32), 4),
                "ef_construct": max(int(getattr(args, "qdrant_hnsw_ef_construct", 128) or 128), 16),
            }
            try:
                update_kwargs["hnsw_config"] = qdrant_models.HnswConfigDiff(
                    **hnsw_kwargs,
                    on_disk=bool(getattr(args, "qdrant_hnsw_on_disk", True)),
                )
            except Exception:
                update_kwargs["hnsw_config"] = qdrant_models.HnswConfigDiff(**hnsw_kwargs)
            applied_features.append("hnsw")
    except Exception as exc:
        errors.append(f"hnsw:{exc}")

    try:
        if hasattr(qdrant_models, "OptimizersConfigDiff"):
            # Lowering indexing_threshold from 20000 to 500 to ensure small collections are indexed
            update_kwargs["optimizers_config"] = qdrant_models.OptimizersConfigDiff(
                indexing_threshold=max(int(getattr(args, "qdrant_optimizer_indexing_threshold", 500) or 500), 100),
                memmap_threshold=max(int(getattr(args, "qdrant_optimizer_memmap_threshold", 50000) or 50000), 1000),
            )
            applied_features.append("optimizers")
    except Exception as exc:
        errors.append(f"optimizers:{exc}")

    quantization_mode = _as_clean_text(getattr(args, "qdrant_quantization", "none")).lower()
    if quantization_mode in {"scalar-int8", "int8", "scalar"}:
        try:
            if all(
                hasattr(qdrant_models, attr)
                for attr in ("ScalarQuantization", "ScalarQuantizationConfig", "ScalarType")
            ):
                update_kwargs["quantization_config"] = qdrant_models.ScalarQuantization(
                    scalar=qdrant_models.ScalarQuantizationConfig(
                        type=qdrant_models.ScalarType.INT8,
                        always_ram=False,
                    )
                )
                applied_features.append("quantization:int8")
            else:
                errors.append("quantization:int8-not-supported-by-client")
        except Exception as exc:
            errors.append(f"quantization:{exc}")

    if not update_kwargs:
        return {"enabled": True, "reason": "no-tuning-config", "applied": False, "features": [], "errors": errors[:10]}

    try:
        client.update_collection(collection_name=collection_name, **update_kwargs)
        return {
            "enabled": True,
            "applied": True,
            "features": applied_features,
            "errors": errors[:10],
        }
    except Exception as exc:
        errors.append(str(exc))
        return {
            "enabled": True,
            "applied": False,
            "features": applied_features,
            "errors": errors[:10],
        }


def ensure_payload_indexes(client, collection_name, index_fields):
    if not index_fields:
        return {"enabled": False, "reason": "no-fields", "created": 0, "errors": []}
    if qdrant_models is None or not hasattr(qdrant_models, "PayloadSchemaType"):
        return {"enabled": False, "reason": "payload-schema-type-unavailable", "created": 0, "errors": []}

    created = 0
    skipped_existing = 0
    errors = []
    keyword_schema = qdrant_models.PayloadSchemaType.KEYWORD

    for field in index_fields:
        try:
            client.create_payload_index(
                collection_name=collection_name,
                field_name=field,
                field_schema=keyword_schema,
            )
            created += 1
        except Exception as exc:
            message = _as_clean_text(exc).lower()
            if any(token in message for token in ("already exists", "exist", "duplicate")):
                skipped_existing += 1
                continue
            errors.append(f"{field}: {exc}")

    return {
        "enabled": True,
        "created": created,
        "skipped_existing": skipped_existing,
        "errors": errors[:10],
    }


def upsert_points_batched(client, collection_name, points, batch_size):
    size = max(int(batch_size or 0), 1)
    chunks = [points[i : i + size] for i in range(0, len(points), size)]
    for chunk in chunks:
        try:
            client.upsert(collection_name=collection_name, points=chunk)
        except Exception:
            # Single retry for transient transport errors.
            client.upsert(collection_name=collection_name, points=chunk)
    return len(chunks)


def retrieve_existing_payload_map(client, collection_name, point_ids, batch_size):
    if not point_ids:
        return {}
    size = max(int(batch_size or 0), 1)
    payload_map = {}
    ids = list(point_ids)
    for i in range(0, len(ids), size):
        batch = ids[i : i + size]
        try:
            records = client.retrieve(
                collection_name=collection_name,
                ids=batch,
                with_payload=True,
                with_vectors=False,
            )
        except Exception:
            continue
        for record in records or []:
            rid = getattr(record, "id", None)
            if rid is None:
                continue
            normalized_id = normalize_qdrant_point_id(rid)
            if normalized_id is None:
                continue
            payload_map[normalized_id] = getattr(record, "payload", {}) or {}
    return payload_map


def normalize_qdrant_point_id(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        return str(value)
    text = _as_clean_text(value)
    if not text:
        return None
    if text.isdigit():
        try:
            return int(text)
        except Exception:
            return text
    return text


def collect_collection_point_ids(client, collection_name, page_size=1024):
    ids = set()
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=collection_name,
            limit=page_size,
            offset=next_offset,
            with_payload=False,
            with_vectors=False,
        )
        for point in points or []:
            pid = getattr(point, "id", None)
            if pid is None:
                continue
            normalized_id = normalize_qdrant_point_id(pid)
            if normalized_id is None:
                continue
            ids.add(normalized_id)
        if next_offset is None:
            break
    return ids


def delete_points_by_ids(client, collection_name, point_ids, batch_size):
    if not point_ids:
        return 0
    deleted = 0
    size = max(int(batch_size or 0), 1)
    ids = list(point_ids)
    for i in range(0, len(ids), size):
        batch = ids[i : i + size]
        try:
            client.delete(collection_name=collection_name, points_selector=batch)
        except Exception:
            if qdrant_models is not None and hasattr(qdrant_models, "PointIdsList"):
                selector = qdrant_models.PointIdsList(points=batch)
                client.delete(collection_name=collection_name, points_selector=selector)
            else:
                raise
        deleted += len(batch)
    return deleted


def collect_collection_metrics(client, collection_name):
    metrics = {
        "collection_name": collection_name,
        "status": "",
        "points_count": 0,
        "indexed_vectors_count": 0,
        "segments_count": 0,
        "vector_index_coverage_percent": 0.0,
        "fragmentation_percent": 0.0,
    }
    try:
        info = client.get_collection(collection_name=collection_name)
    except Exception as exc:
        metrics["status"] = f"error:{exc}"
        return metrics

    for field in ("status", "points_count", "indexed_vectors_count", "segments_count"):
        value = getattr(info, field, None)
        if value is None and hasattr(info, "result"):
            value = getattr(info.result, field, None)
        if value is not None:
            metrics[field] = value

    points_count = 0.0
    indexed_vectors_count = 0.0
    try:
        points_count = float(metrics.get("points_count") or 0.0)
    except Exception:
        points_count = 0.0
    try:
        indexed_vectors_count = float(metrics.get("indexed_vectors_count") or 0.0)
    except Exception:
        indexed_vectors_count = 0.0

    if points_count > 0:
        coverage = min(max((indexed_vectors_count / points_count) * 100.0, 0.0), 100.0)
        metrics["vector_index_coverage_percent"] = round(coverage, 2)
        metrics["fragmentation_percent"] = round(max(0.0, 100.0 - coverage), 2)

    if not metrics.get("status"):
        metrics["status"] = "ok"
    return metrics


def build_qdrant_maintenance_report(metrics, orphan_prune):
    segment_warn_threshold = max(int(os.getenv("QDRANT_SEGMENTS_WARN_THRESHOLD", "96") or 96), 8)
    fragmentation_warn_percent = max(
        float(os.getenv("QDRANT_FRAGMENTATION_WARN_PERCENT", "35.0") or 35.0),
        0.0,
    )

    segments_count = int(metrics.get("segments_count") or 0)
    fragmentation_percent = float(metrics.get("fragmentation_percent") or 0.0)
    points_count = int(metrics.get("points_count") or 0)
    indexed_vectors_count = int(metrics.get("indexed_vectors_count") or 0)
    orphan_deleted = int((orphan_prune or {}).get("deleted_points") or 0)

    recommendations = []
    if segments_count >= segment_warn_threshold:
        recommendations.append(
            f"segments-count-high:{segments_count}>={segment_warn_threshold}"
        )
    if fragmentation_percent >= fragmentation_warn_percent:
        recommendations.append(
            f"fragmentation-high:{fragmentation_percent}>={fragmentation_warn_percent}"
        )
    if points_count > 0 and indexed_vectors_count <= 0:
        recommendations.append("index-coverage-empty")

    return {
        "segment_warn_threshold": segment_warn_threshold,
        "fragmentation_warn_percent": round(fragmentation_warn_percent, 2),
        "segments_count": segments_count,
        "fragmentation_percent": round(fragmentation_percent, 2),
        "vector_index_coverage_percent": float(metrics.get("vector_index_coverage_percent") or 0.0),
        "orphan_deleted_points": orphan_deleted,
        "recommendations": recommendations[:10],
        "maintenance_ok": len(recommendations) == 0,
    }


def _extract_vector_size(value):
    if isinstance(value, (int, float, str)):
        return None

    if isinstance(value, dict):
        size = value.get("size")
        if isinstance(size, int) and size > 0:
            return size
        # Prefer known structural keys from Qdrant collection schema first.
        preferred_keys = ("vectors", "params", "config", "default", "vector")
        for key in preferred_keys:
            if key in value:
                nested_size = _extract_vector_size(value.get(key))
                if nested_size:
                    return nested_size
        # Named vectors can appear as arbitrary keys under "vectors".
        for nested in value.values():
            if isinstance(nested, (dict, list, tuple)) or hasattr(nested, "model_dump") or hasattr(nested, "dict"):
                nested_size = _extract_vector_size(nested)
                if nested_size:
                    return nested_size
        return None

    if isinstance(value, (list, tuple)):
        for nested in value:
            nested_size = _extract_vector_size(nested)
            if nested_size:
                return nested_size
        return None

    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump()
            nested_size = _extract_vector_size(dumped)
            if nested_size:
                return nested_size
        except Exception:
            pass

    if hasattr(value, "dict"):
        try:
            dumped = value.dict()
            nested_size = _extract_vector_size(dumped)
            if nested_size:
                return nested_size
        except Exception:
            pass

    for attr in ("size", "vectors", "vector", "params", "config"):
        if hasattr(value, attr):
            nested_size = _extract_vector_size(getattr(value, attr))
            if nested_size:
                return nested_size

    return None


def get_collection_vector_size(client, collection_name):
    try:
        info = client.get_collection(collection_name)
    except Exception:
        return None
    size = _extract_vector_size(info)
    if isinstance(size, int) and size > 0:
        return size
    return None


def normalize_embedding(vector, target_size):
    raw = []
    for item in vector or []:
        if isinstance(item, (int, float)):
            raw.append(float(item))
        elif isinstance(item, str):
            try:
                raw.append(float(item))
            except ValueError:
                continue
    if target_size <= 0:
        return raw
    if len(raw) > target_size:
        raw = raw[:target_size]
    elif len(raw) < target_size:
        raw.extend([0.0] * (target_size - len(raw)))

    norm = sum(v * v for v in raw) ** 0.5
    if norm > 0:
        raw = [v / norm for v in raw]
    return raw


def build_fallback_embedding(text, vector_size):
    target_size = max(int(vector_size), 8)
    seed = hashlib.sha256((text or "").encode("utf-8")).digest()
    needed = target_size * 4
    buf = bytearray()
    counter = 0
    while len(buf) < needed:
        buf.extend(hashlib.sha256(seed + counter.to_bytes(4, "big")).digest())
        counter += 1

    values = []
    for idx in range(target_size):
        chunk = bytes(buf[idx * 4 : (idx + 1) * 4])
        number = int.from_bytes(chunk, "big", signed=False)
        values.append((number / 0xFFFFFFFF) * 2.0 - 1.0)
    return normalize_embedding(values, target_size)


def build_local_embedding(text, vector_size):
    target_size = max(int(vector_size), 8)
    signal = [0.0] * target_size
    tokens = re.findall(r"[A-Za-z0-9_]+", (text or "").lower())
    if not tokens:
        return build_fallback_embedding(text, target_size)

    for idx, token in enumerate(tokens):
        digest = hashlib.sha256(f"{idx}:{token}".encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:2], "big", signed=False) % target_size
        sign = 1.0 if (digest[2] % 2 == 0) else -1.0
        weight = 0.75 + (digest[3] / 255.0)
        signal[bucket] += sign * weight
    return normalize_embedding(signal, target_size)


def _extract_embedding(payload):
    if not isinstance(payload, dict):
        return None
    direct = payload.get("embedding")
    if isinstance(direct, list) and direct:
        return direct
    batched = payload.get("embeddings")
    if isinstance(batched, list) and batched:
        first = batched[0]
        if isinstance(first, list) and first:
            return first
    openai_data = payload.get("data")
    if isinstance(openai_data, list) and openai_data:
        first_item = openai_data[0]
        if isinstance(first_item, dict):
            openai_vector = first_item.get("embedding")
            if isinstance(openai_vector, list) and openai_vector:
                return openai_vector
    return None


def _safe_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def detect_gpu_total_memory_mb():
    commands = [
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
        [r"C:\Program Files\NVIDIA Corporation\NVSMI\nvidia-smi.exe", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
    ]
    for cmd in commands:
        try:
            completed = subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False)
        except Exception:
            continue
        if completed.returncode != 0:
            continue
        values = []
        for line in (completed.stdout or "").splitlines():
            text = (line or "").strip()
            if not text:
                continue
            try:
                values.append(int(text.split()[0]))
            except Exception:
                continue
        if values:
            return max(values)
    return 0


def resolve_effective_batch_size(args):
    requested = max(_safe_int(getattr(args, "ollama_embed_batch_size", 24), 24), 1)
    auto_enabled = bool(getattr(args, "ollama_embed_batch_size_auto", False))
    gpu_memory_mb = 0
    effective = requested

    if auto_enabled:
        gpu_memory_mb = detect_gpu_total_memory_mb()
        if gpu_memory_mb >= 24000:
            auto_size = 256
        elif gpu_memory_mb >= 16000:
            auto_size = 192
        elif gpu_memory_mb >= 12000:
            auto_size = 128
        elif gpu_memory_mb >= 8000:
            auto_size = 96
        elif gpu_memory_mb >= 6000:
            auto_size = 64
        else:
            auto_size = 32

        model_name = _as_clean_text(getattr(args, "ollama_model", "")).lower()
        if "large" in model_name:
            auto_size = int(auto_size * 0.75)
        if any(token in model_name for token in ("14b", "13b", "12b", "11b", "10b", "9b", "8b")):
            auto_size = int(auto_size * 0.6)
        auto_size = max(auto_size, 16)
        effective = max(requested, auto_size)

    return {
        "requested_batch_size": requested,
        "effective_batch_size": max(effective, 1),
        "auto_enabled": auto_enabled,
        "gpu_memory_mb": gpu_memory_mb,
    }


def resolve_embedding_workload_profile(args, node_count, batch_profile, runtime_info):
    small_threshold = max(_safe_int(os.getenv("OLLAMA_EMBED_SMALL_WORKLOAD_THRESHOLD", "100"), 100), 1)
    small_batch_size = max(_safe_int(os.getenv("OLLAMA_EMBED_SMALL_BATCH_SIZE", "24"), 24), 1)
    small_concurrency_default = max(_safe_int(os.getenv("OLLAMA_EMBED_SMALL_CONCURRENCY", "3"), 3), 1)
    warmup_only_when_cold = str(os.getenv("OLLAMA_EMBED_WARMUP_ONLY_WHEN_COLD", "1")).strip().lower() in {"1", "true", "yes", "on"}

    requested_batch_size = int(batch_profile.get("requested_batch_size", 24) or 24)
    effective_batch_size = int(batch_profile.get("effective_batch_size", requested_batch_size) or requested_batch_size)
    auto_batch_enabled = bool(batch_profile.get("auto_enabled", False))
    configured_concurrency = max(_safe_int(getattr(args, "ollama_embed_concurrency", 4), 4), 1)
    configured_warmup_inputs = max(_safe_int(getattr(args, "ollama_embed_warmup_inputs", 32), 32), 0)
    profile_name = "small" if int(node_count) <= small_threshold else "large"

    is_runtime_warm = (
        isinstance(runtime_info, dict)
        and bool(runtime_info.get("available"))
        and int(runtime_info.get("matched_models", 0) or 0) > 0
        and str(runtime_info.get("processor", "")).strip().lower() == "gpu"
    )

    if profile_name == "small":
        effective_batch_size = min(max(effective_batch_size, 1), small_batch_size)
        auto_batch_enabled = False
        warmup_inputs = 0
        concurrency = max(min(configured_concurrency, 4), 2)
    else:
        warmup_inputs = configured_warmup_inputs
        concurrency = configured_concurrency
        if warmup_only_when_cold and is_runtime_warm:
            warmup_inputs = 0

    return {
        "name": profile_name,
        "node_count": int(node_count),
        "small_threshold": small_threshold,
        "requested_batch_size": requested_batch_size,
        "effective_batch_size": max(int(effective_batch_size), 1),
        "auto_batch_enabled": bool(auto_batch_enabled),
        "concurrency": max(int(concurrency), 1),
        "warmup_inputs": max(int(warmup_inputs), 0),
        "warmup_only_when_cold": bool(warmup_only_when_cold),
        "runtime_warm": bool(is_runtime_warm),
    }


def resolve_ollama_base_url(url):
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        return ""
    for marker in ("/api/", "/v1/"):
        marker_index = normalized.lower().find(marker)
        if marker_index >= 0:
            return normalized[:marker_index]
    return normalized


def inspect_ollama_runtime(url, model):
    base_url = resolve_ollama_base_url(url)
    runtime = {
        "available": False,
        "processor": "unknown",
        "reason": "",
        "endpoint": "",
        "model_requested": _as_clean_text(model),
        "model_selected": "",
        "matched_models": 0,
        "running_models": 0,
        "size_vram_bytes": 0,
        "size_bytes": 0,
    }
    if not base_url:
        runtime["reason"] = "empty-ollama-url"
        return runtime

    endpoint = f"{base_url}/api/ps"
    runtime["endpoint"] = endpoint
    try:
        response = requests.get(endpoint, timeout=15)
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        runtime["reason"] = str(exc)
        return runtime

    runtime["available"] = True
    if not isinstance(payload, dict):
        runtime["reason"] = "invalid-runtime-payload"
        return runtime

    models = payload.get("models")
    if not isinstance(models, list):
        runtime["reason"] = "runtime-model-list-missing"
        return runtime

    running_models = [item for item in models if isinstance(item, dict)]
    runtime["running_models"] = len(running_models)
    if not running_models:
        runtime["reason"] = "no-running-models"
        return runtime

    requested = _as_clean_text(model).lower()
    matched = []
    for item in running_models:
        name = _as_clean_text(item.get("name")).lower()
        if not requested:
            matched.append(item)
            continue
        if name == requested or name.startswith(requested) or requested in name:
            matched.append(item)

    runtime["matched_models"] = len(matched)
    if requested and not matched:
        runtime["reason"] = "requested-model-not-running"
        return runtime

    selected = matched[0] if matched else running_models[0]
    runtime["model_selected"] = _as_clean_text(selected.get("name"))
    runtime["size_vram_bytes"] = _safe_int(selected.get("size_vram"), 0)
    runtime["size_bytes"] = _safe_int(selected.get("size"), 0)
    runtime["processor"] = "gpu" if runtime["size_vram_bytes"] > 0 else "cpu"
    runtime["reason"] = "detected"
    return runtime


def _build_embedding_endpoints(url):
    normalized = (url or "").strip().rstrip("/")
    if not normalized:
        return []

    base_url = resolve_ollama_base_url(normalized)
    endpoints = [normalized]
    if normalized.endswith("/api/embeddings"):
        endpoints.append(normalized[: -len("/api/embeddings")] + "/api/embed")
        endpoints.append(normalized[: -len("/api/embeddings")] + "/v1/embeddings")
    elif normalized.endswith("/api/embed"):
        endpoints.append(normalized[: -len("/api/embed")] + "/api/embeddings")
        endpoints.append(normalized[: -len("/api/embed")] + "/v1/embeddings")
    elif normalized.endswith("/v1/embeddings"):
        endpoints.append(normalized[: -len("/v1/embeddings")] + "/api/embeddings")
        endpoints.append(normalized[: -len("/v1/embeddings")] + "/api/embed")
    elif base_url:
        endpoints.append(f"{base_url}/api/embeddings")
        endpoints.append(f"{base_url}/api/embed")
        endpoints.append(f"{base_url}/v1/embeddings")

    seen = set()
    ordered = []
    for endpoint in endpoints:
        if endpoint and endpoint not in seen:
            seen.add(endpoint)
            ordered.append(endpoint)
    return ordered


def _extract_embeddings(payload):
    if not isinstance(payload, dict):
        return None
    batched = payload.get("embeddings")
    if isinstance(batched, list) and batched:
        vectors = [entry for entry in batched if isinstance(entry, list) and entry]
        if vectors:
            return vectors
    openai_data = payload.get("data")
    if isinstance(openai_data, list) and openai_data:
        vectors = []
        for item in openai_data:
            if not isinstance(item, dict):
                continue
            vector = item.get("embedding")
            if isinstance(vector, list) and vector:
                vectors.append(vector)
        if vectors:
            return vectors
    direct = payload.get("embedding")
    if isinstance(direct, list) and direct:
        return [direct]
    return None


def request_embeddings_batch(url, model, texts, keep_alive, stats=None):
    normalized_texts = [str(item or "") for item in (texts or [])]
    if not normalized_texts:
        return []
    if len(normalized_texts) == 1:
        return [request_embedding(url, model, normalized_texts[0], keep_alive, stats=stats)]

    ordered_endpoints = _build_embedding_endpoints(url)
    if not ordered_endpoints:
        raise RuntimeError("Ollama embedding URL is empty.")

    last_error = None
    for endpoint in ordered_endpoints:
        supports_batch_input = endpoint.endswith("/api/embed") or endpoint.endswith("/v1/embeddings")
        if not supports_batch_input:
            continue
        payload = {"model": model, "input": normalized_texts}
        keep_alive_value = _as_clean_text(keep_alive)
        if keep_alive_value:
            payload["keep_alive"] = keep_alive_value
        try:
            response = requests.post(endpoint, json=payload, timeout=180)
            if response.status_code == 404:
                if last_error is None:
                    last_error = RuntimeError(f"endpoint-not-found: {endpoint}")
                continue
            if response.status_code in (400, 422):
                last_error = RuntimeError(f"endpoint-batch-rejected:{endpoint}:{response.status_code}")
                continue
            response.raise_for_status()
            vectors = _extract_embeddings(response.json())
            if vectors and len(vectors) >= len(normalized_texts):
                if isinstance(stats, dict):
                    stats["batch_requests"] = int(stats.get("batch_requests", 0)) + 1
                    stats["batch_items"] = int(stats.get("batch_items", 0)) + len(normalized_texts)
                return vectors[: len(normalized_texts)]
            if vectors and len(vectors) == 1:
                if isinstance(stats, dict):
                    stats["batch_requests"] = int(stats.get("batch_requests", 0)) + 1
                    stats["batch_items"] = int(stats.get("batch_items", 0)) + len(normalized_texts)
                return [vectors[0] for _ in normalized_texts]
            last_error = RuntimeError(f"embedding-batch-missing-or-size-mismatch: {endpoint}")
        except requests.RequestException as exc:
            last_error = exc
            continue

    # Fallback: per-item requests keep Ollama usage even when batch API shape is not supported.
    vectors = []
    success_count = 0
    for text in normalized_texts:
        try:
            vector = request_embedding(url, model, text, keep_alive, stats=stats)
            vectors.append(vector)
            if vector:
                success_count += 1
        except Exception:
            vectors.append(None)
    if success_count > 0:
        return vectors

    raise RuntimeError(f"Failed batch embedding request for {len(normalized_texts)} items: {last_error}")


def request_embedding(url, model, text, keep_alive, stats=None):
    ordered_endpoints = _build_embedding_endpoints(url)
    if not ordered_endpoints:
        raise RuntimeError("Ollama embedding URL is empty.")

    base_text = _as_clean_text(text)
    candidate_texts = [base_text]
    for cap in (1200, 700, 400, 240):
        if len(base_text) > cap:
            candidate_texts.append(base_text[:cap].rstrip())

    last_error = None
    for candidate_text in candidate_texts:
        for endpoint in ordered_endpoints:
            payload = {"model": model}
            keep_alive_value = _as_clean_text(keep_alive)
            if keep_alive_value:
                payload["keep_alive"] = keep_alive_value
            if endpoint.endswith("/api/embed") or endpoint.endswith("/v1/embeddings"):
                payload["input"] = candidate_text
            else:
                payload["prompt"] = candidate_text

            try:
                response = requests.post(endpoint, json=payload, timeout=120)
                if response.status_code == 404:
                    if last_error is None:
                        last_error = RuntimeError(f"endpoint-not-found: {endpoint}")
                    continue
                response.raise_for_status()
                embedding = _extract_embedding(response.json())
                if embedding:
                    if isinstance(stats, dict):
                        stats["single_requests"] = int(stats.get("single_requests", 0)) + 1
                    return embedding
                last_error = RuntimeError(f"embedding-missing-in-response: {endpoint}")
            except requests.RequestException as exc:
                last_error = exc
                continue

    raise RuntimeError(f"Failed to obtain embedding from Ollama endpoints {ordered_endpoints}: {last_error}")


def build_embedding_text(node):
    summary = _as_clean_text(node.get("summary"))
    details = _as_clean_text(node.get("details"))
    node_id = _as_clean_text(node.get("id"))
    node_type = _as_clean_text(node.get("node_type")).lower()
    relative_path = _as_clean_text(node.get("relative_path"))
    raw_content = _as_clean_text(node.get("raw_content"))

    parts = []
    if node_type:
        parts.append(f"type: {node_type}")
    if node_id:
        parts.append(f"id: {node_id}")
    if relative_path:
        parts.append(f"path: {relative_path}")
    if summary:
        parts.append(f"summary: {summary}")
    if details:
        parts.append(f"details: {details}")
    if raw_content and len(parts) <= 2:
        # Keep only a short excerpt to avoid context-limit failures on small embedding models.
        parts.append(f"excerpt: {trim_text_for_embedding(raw_content, 900)}")

    if parts:
        return "\n".join(parts)
    if node_id:
        return node_id
    return "memory-node"


def sync_qdrant(nodes, args):
    if args.skip_qdrant:
        return {"enabled": False, "reason": "skip flag"}
    if QdrantClient is None or qdrant_models is None:
        return {"enabled": False, "reason": "qdrant-client not installed"}
    if not nodes:
        return {"enabled": False, "reason": "no nodes"}

    prefix_value = (args.collection_prefix or "").strip().strip("-_")
    if prefix_value and prefix_value.lower() != args.project_slug.lower():
        collection = f"{prefix_value}-{args.project_slug}-memory"
    else:
        collection = f"{args.project_slug}-memory"
    upsert_batch_size = max(int(getattr(args, "qdrant_upsert_batch_size", 512) or 512), 1)
    payload_index_enabled = not bool(getattr(args, "qdrant_disable_payload_indexes", False))
    payload_index_fields = parse_csv_list(getattr(args, "qdrant_index_fields", ""))
    try:
        client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port, check_compatibility=False)
    except TypeError:
        client = QdrantClient(host=args.qdrant_host, port=args.qdrant_port)

    try:
        existing_collection_names = {c.name for c in client.get_collections().collections}
    except Exception:
        existing_collection_names = set()

    collection_exists = collection in existing_collection_names
    existing_vector_size = get_collection_vector_size(client, collection)
    target_vector_size = existing_vector_size
    vector_size_source = "existing_collection" if existing_vector_size else "runtime"
    embedding_started_at = time.perf_counter()
    runtime_info = inspect_ollama_runtime(args.ollama_url, args.ollama_model)
    batch_profile = resolve_effective_batch_size(args)
    workload_profile = resolve_embedding_workload_profile(args, len(nodes), batch_profile, runtime_info)
    batch_size = int(workload_profile.get("effective_batch_size", 24))
    batch_size_requested = int(workload_profile.get("requested_batch_size", batch_size))
    batch_size_auto_enabled = bool(workload_profile.get("auto_batch_enabled", False))
    batch_size_gpu_memory_mb = int(batch_profile.get("gpu_memory_mb", 0))
    embed_concurrency = max(int(workload_profile.get("concurrency", 4)), 1)
    warmup_inputs = max(int(workload_profile.get("warmup_inputs", 0)), 0)
    embed_max_chars = max(int(getattr(args, "ollama_embed_max_chars", 4000) or 4000), 0)

    precomputed = {}
    embedding_errors = []
    source_counts = {"ollama": 0, "local_hash_projection": 0, "fallback_hash": 0}
    embedding_transport_stats = {
        "batch_requests": 0,
        "batch_items": 0,
        "single_requests": 0,
        "batch_workers_used": 1,
        "batch_chunk_size": batch_size,
        "warmup_inputs": warmup_inputs,
        "warmup_ran": False,
        "warmup_failed": False,
    }
    payload_validation_errors = []
    skipped_points = 0
    skipped_unchanged_points = 0

    incremental_enabled = bool(getattr(args, "qdrant_incremental_sync", False)) and (not bool(getattr(args, "qdrant_disable_incremental_sync", False)))
    node_hash_by_id = {}
    project_point_id_by_node_id = {}
    all_project_point_ids = []
    for node in nodes:
        node_id = _as_clean_text(node.get("id")) or _as_clean_text(node.get("relative_path")) or "unknown-node"
        node_hash_by_id[node_id] = compute_node_content_hash(node)
        pid = int(hashlib.sha256(f"{args.project_slug}:{node_id}".encode("utf-8")).hexdigest()[:16], 16)
        project_point_id_by_node_id[node_id] = pid
        all_project_point_ids.append(pid)

    existing_project_payload_map = {}
    if incremental_enabled and collection_exists and all_project_point_ids:
        existing_project_payload_map = retrieve_existing_payload_map(
            client=client,
            collection_name=collection,
            point_ids=all_project_point_ids,
            batch_size=upsert_batch_size,
        )
    unchanged_node_ids = set()
    if incremental_enabled and existing_project_payload_map:
        for node_id, pid in project_point_id_by_node_id.items():
            payload = existing_project_payload_map.get(pid)
            if not isinstance(payload, dict):
                continue
            existing_hash = _as_clean_text(payload.get("content_hash"))
            if existing_hash and existing_hash == _as_clean_text(node_hash_by_id.get(node_id)):
                unchanged_node_ids.add(node_id)

    if target_vector_size is None:
        first_node = nodes[0]
        first_node_id = _as_clean_text(first_node.get("id")) or _as_clean_text(first_node.get("relative_path")) or "first-node"
        first_node_text = trim_text_for_embedding(build_embedding_text(first_node), embed_max_chars)
        try:
            first_embedding = request_embedding(
                args.ollama_url,
                args.ollama_model,
                first_node_text,
                args.ollama_keep_alive,
                stats=embedding_transport_stats,
            )
            if first_embedding:
                target_vector_size = len(first_embedding)
                precomputed[first_node_id] = {"vector": first_embedding, "source": "ollama"}
                vector_size_source = "ollama"
        except Exception as exc:
            embedding_errors.append(str(exc))
            target_vector_size = max(int(args.qdrant_vector_size), 8)
            first_local = build_local_embedding(first_node_text, target_vector_size)
            precomputed[first_node_id] = {"vector": first_local, "source": "local_hash_projection"}
            vector_size_source = "local_fallback_default"

    if target_vector_size is None or target_vector_size <= 0:
        target_vector_size = max(int(args.qdrant_vector_size), 8)
        vector_size_source = "fallback_default"

    if warmup_inputs > 0:
        warmup_payload_size = max(min(warmup_inputs, batch_size), 1)
        warmup_text = "gpu warmup embedding payload for orchestrator memory sync"
        try:
            _ = request_embeddings_batch(
                args.ollama_url,
                args.ollama_model,
                [warmup_text] * warmup_payload_size,
                args.ollama_keep_alive,
                stats=None,
            )
            embedding_transport_stats["warmup_ran"] = True
            embedding_transport_stats["batch_requests"] = int(embedding_transport_stats.get("batch_requests", 0)) + 1
            embedding_transport_stats["batch_items"] = int(embedding_transport_stats.get("batch_items", 0)) + warmup_payload_size
        except Exception as warmup_exc:
            embedding_transport_stats["warmup_failed"] = True
            embedding_errors.append(f"warmup:{warmup_exc}")

    pending = []
    for node in nodes:
        node_id = _as_clean_text(node.get("id")) or _as_clean_text(node.get("relative_path")) or "unknown-node"
        if node_id in unchanged_node_ids:
            continue
        if node_id in precomputed:
            continue
        pending.append((node_id, trim_text_for_embedding(build_embedding_text(node), embed_max_chars)))

    if batch_size > 1 and pending:
        chunk_size = batch_size
        if embed_concurrency > 1:
            initial_chunks = max((len(pending) + batch_size - 1) // batch_size, 1)
            minimum_parallel_items = embed_concurrency * 16
            if initial_chunks < embed_concurrency and len(pending) >= minimum_parallel_items:
                target_chunks = min(embed_concurrency, len(pending))
                if target_chunks > 1:
                    concurrency_chunk = max((len(pending) + target_chunks - 1) // target_chunks, 1)
                    chunk_size = min(batch_size, concurrency_chunk)
        embedding_transport_stats["batch_chunk_size"] = chunk_size
        chunks = [pending[i : i + chunk_size] for i in range(0, len(pending), chunk_size)]
        failed_chunks = []
        workers = max(min(embed_concurrency, len(chunks)), 1)
        embedding_transport_stats["batch_workers_used"] = workers

        if workers > 1 and len(chunks) > 1:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                future_map = {
                    executor.submit(
                        request_embeddings_batch,
                        args.ollama_url,
                        args.ollama_model,
                        [entry[1] for entry in chunk],
                        args.ollama_keep_alive,
                        None,
                    ): chunk
                    for chunk in chunks
                }

                for future in concurrent.futures.as_completed(future_map):
                    chunk = future_map[future]
                    try:
                        vectors = future.result()
                        embedding_transport_stats["batch_requests"] = int(embedding_transport_stats.get("batch_requests", 0)) + 1
                        embedding_transport_stats["batch_items"] = int(embedding_transport_stats.get("batch_items", 0)) + len(chunk)
                        for index, vector in enumerate(vectors):
                            if index >= len(chunk):
                                break
                            chunk_node_id = chunk[index][0]
                            if vector:
                                precomputed[chunk_node_id] = {"vector": vector, "source": "ollama"}
                    except Exception as batch_exc:
                        embedding_errors.append(str(batch_exc))
                        failed_chunks.append(chunk)
        else:
            for chunk in chunks:
                texts = [entry[1] for entry in chunk]
                try:
                    vectors = request_embeddings_batch(
                        args.ollama_url,
                        args.ollama_model,
                        texts,
                        args.ollama_keep_alive,
                        stats=None,
                    )
                    embedding_transport_stats["batch_requests"] = int(embedding_transport_stats.get("batch_requests", 0)) + 1
                    embedding_transport_stats["batch_items"] = int(embedding_transport_stats.get("batch_items", 0)) + len(chunk)
                    for index, vector in enumerate(vectors):
                        if index >= len(chunk):
                            break
                        chunk_node_id = chunk[index][0]
                        if vector:
                            precomputed[chunk_node_id] = {"vector": vector, "source": "ollama"}
                except Exception as batch_exc:
                    embedding_errors.append(str(batch_exc))
                    failed_chunks.append(chunk)

        for chunk in failed_chunks:
            for chunk_node_id, chunk_text in chunk:
                if chunk_node_id in precomputed:
                    continue
                try:
                    vector = request_embedding(
                        args.ollama_url,
                        args.ollama_model,
                        chunk_text,
                        args.ollama_keep_alive,
                        stats=embedding_transport_stats,
                    )
                    if vector:
                        precomputed[chunk_node_id] = {"vector": vector, "source": "ollama"}
                except Exception as single_exc:
                    embedding_errors.append(str(single_exc))

    if vector_size_source != "existing_collection":
        for item in precomputed.values():
            if not isinstance(item, dict):
                continue
            if _as_clean_text(item.get("source")).lower() != "ollama":
                continue
            vector = item.get("vector")
            if isinstance(vector, list) and vector:
                target_vector_size = len(vector)
                vector_size_source = "ollama"
                break

    points = []
    global_points = []
    valid_project_point_ids = []
    global_node_types = parse_csv_set(args.global_node_types)
    global_sync_enabled = not bool(args.disable_global_sync)
    global_collection_name = _as_clean_text(args.global_collection) or "orchestrator-global-memory"
    global_skipped_by_type = 0
    global_skipped_sensitive = 0
    global_skipped_payload = 0
    global_skipped_unchanged = 0
    for node in nodes:
        node_id = _as_clean_text(node.get("id")) or _as_clean_text(node.get("relative_path")) or "unknown-node"
        embedding_text = trim_text_for_embedding(build_embedding_text(node), embed_max_chars)
        node_slug = _as_clean_text(node.get("project_slug")).lower()
        if not node_slug:
            raise RuntimeError(f"memory-node-without-project_slug blocked: node_id={node_id}")
        if node_slug != args.project_slug.lower():
            raise RuntimeError(
                f"memory-node-project_slug-mismatch blocked: node_id={node_id} node_slug={node_slug} expected={args.project_slug.lower()}"
            )

        content_hash = _as_clean_text(node_hash_by_id.get(node_id)) or compute_node_content_hash(node)
        payload = build_qdrant_payload(
            node=node,
            project_slug=args.project_slug,
            embedding_source="pending",
            content_hash=content_hash,
        )
        violations = validate_qdrant_payload(payload, args.project_slug)
        if violations:
            skipped_points += 1
            payload_validation_errors.append(
                f"{payload.get('node_id', '<unknown-node>')}: missing/empty {', '.join(violations)}"
            )
            continue

        pid = int(project_point_id_by_node_id.get(node_id) or int(hashlib.sha256(f"{args.project_slug}:{payload.get('node_id', node_id)}".encode("utf-8")).hexdigest()[:16], 16))
        existing_payload = existing_project_payload_map.get(pid) if incremental_enabled else None
        valid_project_point_ids.append(pid)
        if isinstance(existing_payload, dict):
            existing_hash = _as_clean_text(existing_payload.get("content_hash"))
            if existing_hash and existing_hash == content_hash:
                skipped_unchanged_points += 1
                if global_sync_enabled:
                    global_skipped_unchanged += 1
                continue

        precomputed_item = precomputed.get(node_id)
        emb = None
        embedding_source = "ollama"
        if isinstance(precomputed_item, dict):
            emb = precomputed_item.get("vector")
            source_value = _as_clean_text(precomputed_item.get("source")).lower()
            if source_value in {"ollama", "local_hash_projection", "fallback_hash"}:
                embedding_source = source_value
        elif precomputed_item is not None:
            emb = precomputed_item

        if emb is None:
            try:
                emb = request_embedding(
                    args.ollama_url,
                    args.ollama_model,
                    embedding_text,
                    args.ollama_keep_alive,
                    stats=embedding_transport_stats,
                )
            except Exception as exc:
                embedding_errors.append(str(exc))
                emb = build_local_embedding(embedding_text, target_vector_size)
                embedding_source = "local_hash_projection"

        emb = normalize_embedding(emb, target_vector_size)
        if not emb:
            emb = build_fallback_embedding(embedding_text, target_vector_size)
            embedding_source = "fallback_hash"

        payload["embedding_source"] = embedding_source
        points.append(qdrant_models.PointStruct(id=pid, vector=emb, payload=payload))
        source_counts[embedding_source] = source_counts.get(embedding_source, 0) + 1

        if not global_sync_enabled:
            continue

        payload_node_type = _as_clean_text(payload.get("node_type")).lower()
        if global_node_types and payload_node_type not in global_node_types:
            global_skipped_by_type += 1
            continue
        if is_sensitive_for_global(payload):
            global_skipped_sensitive += 1
            continue

        global_payload = build_global_qdrant_payload(
            payload=payload,
            project_slug=args.project_slug,
            max_details_chars=args.global_max_details_chars,
        )
        global_violations = validate_qdrant_payload(global_payload, args.project_slug)
        if global_violations:
            global_skipped_payload += 1
            continue
        global_point_id = int(
            hashlib.sha256(
                f"global:{args.project_slug}:{global_payload.get('node_id', node_id)}".encode("utf-8")
            ).hexdigest()[:16],
            16,
        )
        global_points.append(
            qdrant_models.PointStruct(
                id=global_point_id,
                vector=emb,
                payload=global_payload,
            )
        )

    runtime_info_post = inspect_ollama_runtime(args.ollama_url, args.ollama_model)
    if isinstance(runtime_info_post, dict) and runtime_info_post.get("available"):
        runtime_info = runtime_info_post
    fallback_count = int(source_counts.get("fallback_hash", 0))
    local_count = int(source_counts.get("local_hash_projection", 0))
    ollama_count = int(source_counts.get("ollama", 0))
    non_ollama_count = fallback_count + local_count
    embedded_total_count = ollama_count + non_ollama_count
    embedding_generation_seconds = max(time.perf_counter() - embedding_started_at, 0.0001)
    embedding_vectors_per_second = round(embedded_total_count / embedding_generation_seconds, 2)
    ollama_vectors_per_second = round(ollama_count / embedding_generation_seconds, 2) if ollama_count > 0 else 0.0

    if not points:
        only_incremental_skips = skipped_unchanged_points > 0 and skipped_points == 0
        collection_metrics = collect_collection_metrics(client, collection) if collection_exists else {}
        maintenance_report = build_qdrant_maintenance_report(
            collection_metrics,
            {"deleted_points": 0},
        )
        if global_sync_enabled:
            global_sync = {
                "enabled": False if not only_incremental_skips else True,
                "collection_name": global_collection_name,
                "reason": "no-project-points" if not only_incremental_skips else "incremental-no-changes",
                "nodes_synced": 0,
                "skipped_by_node_type": global_skipped_by_type,
                "skipped_sensitive": global_skipped_sensitive,
                "skipped_payload_validation": global_skipped_payload,
                "skipped_unchanged": global_skipped_unchanged,
            }
        else:
            global_sync = {"enabled": False, "reason": "disabled-by-flag", "nodes_synced": 0}
        return {
            "enabled": bool(only_incremental_skips),
            "reason": "incremental-no-changes" if only_incremental_skips else "qdrant-payload-validation-rejected-all-points",
            "payload_schema_validated": True,
            "payload_schema_errors": payload_validation_errors[:10],
            "fallback_embeddings": fallback_count,
            "local_embeddings": local_count,
            "ollama_embeddings": ollama_count,
            "non_ollama_embeddings": non_ollama_count,
            "embedding_batch_size_requested": batch_size_requested,
            "embedding_batch_size": batch_size,
            "embedding_batch_size_auto_enabled": batch_size_auto_enabled,
            "embedding_batch_size_gpu_memory_mb": batch_size_gpu_memory_mb,
            "embedding_workload_profile": workload_profile.get("name", "unknown"),
            "embedding_workload_node_count": int(workload_profile.get("node_count", len(nodes))),
            "embedding_workload_small_threshold": int(workload_profile.get("small_threshold", 100)),
            "embedding_warmup_only_when_cold": bool(workload_profile.get("warmup_only_when_cold", True)),
            "embedding_concurrency": embed_concurrency,
            "embedding_batch_workers_used": int(embedding_transport_stats.get("batch_workers_used", 1)),
            "embedding_batch_chunk_size": int(embedding_transport_stats.get("batch_chunk_size", batch_size)),
            "embedding_batch_requests": int(embedding_transport_stats.get("batch_requests", 0)),
            "embedding_batch_items": int(embedding_transport_stats.get("batch_items", 0)),
            "embedding_single_requests": int(embedding_transport_stats.get("single_requests", 0)),
            "embedding_warmup_inputs": int(embedding_transport_stats.get("warmup_inputs", warmup_inputs)),
            "embedding_warmup_ran": bool(embedding_transport_stats.get("warmup_ran", False)),
            "embedding_warmup_failed": bool(embedding_transport_stats.get("warmup_failed", False)),
            "embedding_generation_seconds": round(embedding_generation_seconds, 3),
            "embedding_vectors_per_second": embedding_vectors_per_second,
            "ollama_vectors_per_second": ollama_vectors_per_second,
            "embedding_runtime": runtime_info,
            "embedding_runtime_processor": runtime_info.get("processor", "unknown"),
            "nodes_synced": 0,
            "nodes_skipped_unchanged": skipped_unchanged_points,
            "nodes_skipped_payload_validation": skipped_points,
            "global_sync": global_sync,
            "qdrant_upsert_batch_size": upsert_batch_size,
            "payload_indexes_enabled": payload_index_enabled,
            "payload_index_fields": payload_index_fields,
            "qdrant_incremental_sync_enabled": incremental_enabled,
            "qdrant_collection_metrics": collection_metrics,
            "qdrant_maintenance": maintenance_report,
        }

    collection_vector_size = ensure_collection(client, collection, target_vector_size)
    tuning_result = apply_collection_tuning(client, collection, args)
    payload_index_result = {"enabled": False, "reason": "disabled-by-flag", "created": 0, "skipped_existing": 0, "errors": []}
    if payload_index_enabled:
        payload_index_result = ensure_payload_indexes(client, collection, payload_index_fields)
    project_upsert_batches = upsert_points_batched(client, collection, points, upsert_batch_size)
    project_metrics = collect_collection_metrics(client, collection)

    orphan_prune = {"enabled": False, "scanned_points": 0, "deleted_points": 0, "errors": []}
    if bool(getattr(args, "qdrant_prune_orphans", False)):
        orphan_prune["enabled"] = True
        try:
            existing_ids = collect_collection_point_ids(client, collection, page_size=min(max(upsert_batch_size, 256), 4096))
            live_ids = set(valid_project_point_ids)
            orphan_ids = sorted(existing_ids - live_ids)
            orphan_prune["scanned_points"] = len(existing_ids)
            if orphan_ids:
                orphan_prune["deleted_points"] = delete_points_by_ids(client, collection, orphan_ids, upsert_batch_size)
        except Exception as prune_exc:
            orphan_prune["errors"] = [str(prune_exc)]

    maintenance_report = build_qdrant_maintenance_report(project_metrics, orphan_prune)

    if global_sync_enabled and global_points:
        ensure_collection(client, global_collection_name, target_vector_size)
        apply_collection_tuning(client, global_collection_name, args)
        if payload_index_enabled:
            ensure_payload_indexes(client, global_collection_name, payload_index_fields)
        global_upsert_batches = upsert_points_batched(client, global_collection_name, global_points, upsert_batch_size)
        global_sync = {
            "enabled": True,
            "collection_name": global_collection_name,
            "nodes_synced": len(global_points),
            "skipped_by_node_type": global_skipped_by_type,
            "skipped_sensitive": global_skipped_sensitive,
            "skipped_payload_validation": global_skipped_payload,
            "skipped_unchanged": global_skipped_unchanged,
            "upsert_batches": global_upsert_batches,
            "collection_metrics": collect_collection_metrics(client, global_collection_name),
        }
    elif global_sync_enabled:
        global_sync = {
            "enabled": False,
            "collection_name": global_collection_name,
            "reason": "no-eligible-points",
            "nodes_synced": 0,
            "skipped_by_node_type": global_skipped_by_type,
            "skipped_sensitive": global_skipped_sensitive,
            "skipped_payload_validation": global_skipped_payload,
            "skipped_unchanged": global_skipped_unchanged,
        }
    else:
        global_sync = {"enabled": False, "reason": "disabled-by-flag", "nodes_synced": 0}

    result = {
        "enabled": True,
        "collection_name": collection,
        "nodes_input": len(nodes),
        "nodes_synced": len(points),
        "nodes_skipped_unchanged": skipped_unchanged_points,
        "nodes_skipped_payload_validation": skipped_points,
        "vector_size": collection_vector_size,
        "vector_size_source": vector_size_source,
        "fallback_embeddings": fallback_count,
        "local_embeddings": local_count,
        "ollama_embeddings": ollama_count,
        "non_ollama_embeddings": non_ollama_count,
        "embedding_batch_size_requested": batch_size_requested,
        "embedding_batch_size": batch_size,
        "embedding_batch_size_auto_enabled": batch_size_auto_enabled,
        "embedding_batch_size_gpu_memory_mb": batch_size_gpu_memory_mb,
        "embedding_workload_profile": workload_profile.get("name", "unknown"),
        "embedding_workload_node_count": int(workload_profile.get("node_count", len(nodes))),
        "embedding_workload_small_threshold": int(workload_profile.get("small_threshold", 100)),
        "embedding_warmup_only_when_cold": bool(workload_profile.get("warmup_only_when_cold", True)),
        "embedding_concurrency": embed_concurrency,
        "embedding_batch_workers_used": int(embedding_transport_stats.get("batch_workers_used", 1)),
        "embedding_batch_chunk_size": int(embedding_transport_stats.get("batch_chunk_size", batch_size)),
        "embedding_batch_requests": int(embedding_transport_stats.get("batch_requests", 0)),
        "embedding_batch_items": int(embedding_transport_stats.get("batch_items", 0)),
        "embedding_single_requests": int(embedding_transport_stats.get("single_requests", 0)),
        "embedding_warmup_inputs": int(embedding_transport_stats.get("warmup_inputs", warmup_inputs)),
        "embedding_warmup_ran": bool(embedding_transport_stats.get("warmup_ran", False)),
        "embedding_warmup_failed": bool(embedding_transport_stats.get("warmup_failed", False)),
        "embedding_generation_seconds": round(embedding_generation_seconds, 3),
        "embedding_vectors_per_second": embedding_vectors_per_second,
        "ollama_vectors_per_second": ollama_vectors_per_second,
        "fallback_ratio_percent": round((fallback_count / len(points)) * 100, 2) if points else 0.0,
        "local_ratio_percent": round((local_count / len(points)) * 100, 2) if points else 0.0,
        "non_ollama_ratio_percent": round((non_ollama_count / len(points)) * 100, 2) if points else 0.0,
        "embedding_runtime": runtime_info,
        "embedding_runtime_processor": runtime_info.get("processor", "unknown"),
        "payload_schema_validated": True,
        "payload_schema_errors": payload_validation_errors[:10],
        "payload_schema_skipped_points": skipped_points,
        "qdrant_incremental_sync_enabled": incremental_enabled,
        "qdrant_upsert_batch_size": upsert_batch_size,
        "qdrant_upsert_batches": project_upsert_batches,
        "qdrant_collection_tuning": tuning_result,
        "qdrant_collection_metrics": project_metrics,
        "qdrant_maintenance": maintenance_report,
        "qdrant_orphan_prune": orphan_prune,
        "payload_indexes_enabled": payload_index_enabled,
        "payload_index_fields": payload_index_fields,
        "payload_index_created": int(payload_index_result.get("created", 0)),
        "payload_index_skipped_existing": int(payload_index_result.get("skipped_existing", 0)),
        "payload_index_errors": payload_index_result.get("errors", []),
        "global_sync": global_sync,
    }
    if embedding_errors:
        result["embedding_errors_sample"] = embedding_errors[:5]
    return result


class Neo4jManager:
    def __init__(self, uri, user, password, database):
        if GraphDatabase is None:
            raise RuntimeError("neo4j driver is not installed")
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = (database or "neo4j").strip() or "neo4j"

    def close(self):
        self.driver.close()

    def run(self, query, **params):
        with self.driver.session(database=self.database) as session:
            session.execute_write(lambda tx: tx.run(query, **params))


def chunk_rows(items, size):
    if size <= 0:
        size = 500
    return [items[idx : idx + size] for idx in range(0, len(items), size)]


def merge_node_records(existing, candidate):
    if not existing:
        return candidate
    existing_props = dict(existing.get("props", {}))
    candidate_props = dict(candidate.get("props", {}))
    existing_score = len(_as_clean_text(existing_props.get("summary"))) + len(_as_clean_text(existing_props.get("details")))
    candidate_score = len(_as_clean_text(candidate_props.get("summary"))) + len(_as_clean_text(candidate_props.get("details")))
    merged = dict(existing)
    merged_props = dict(existing_props)
    if candidate_score > existing_score:
        merged["label"] = candidate.get("label", merged.get("label", DEFAULT_NODE_LABEL))
        merged_props.update(candidate_props)
    else:
        if merged.get("label", DEFAULT_NODE_LABEL) == DEFAULT_NODE_LABEL:
            merged["label"] = candidate.get("label", merged.get("label", DEFAULT_NODE_LABEL))
        for key, value in candidate_props.items():
            if key not in merged_props or merged_props.get(key) in ("", None, [], {}):
                merged_props[key] = value
    merged["props"] = merged_props
    return merged


def build_neo4j_batches(all_nodes, relationships, project_slug):
    node_map = {}
    rel_records = []
    rel_seen = set()

    for raw in all_nodes:
        nid = _as_clean_text(raw.get("id"))
        if not nid:
            continue
        node_slug = _as_clean_text(raw.get("project_slug")).lower() or project_slug.lower()
        if node_slug != project_slug.lower():
            raise RuntimeError(
                f"neo4j-write-blocked: node_project_slug_mismatch id={nid} node_slug={node_slug} expected={project_slug.lower()}"
            )

        label = normalize_label(raw.get("label", raw.get("node_type", "unknown")))
        node_type = _as_clean_text(raw.get("node_type")).lower() or "unknown"
        relative_path = normalize_repo_path(raw.get("relative_path"))
        source_path = normalize_repo_path(raw.get("source_path")) or relative_path
        source_files = _normalize_text_list(raw.get("source_files"))
        source_modules = _normalize_text_list(raw.get("source_modules"))
        module_name = normalize_module_name(_as_clean_text(raw.get("module_name"))) if _as_clean_text(raw.get("module_name")) else ""
        explicit_module_node_id = _as_clean_text(raw.get("module_node_id"))
        explicit_source_file_node_id = _as_clean_text(raw.get("source_file_node_id"))
        if not source_modules and module_name:
            source_modules = [module_name]
        if not source_modules and source_files:
            source_modules = _derive_source_modules(source_files)
        if not module_name and explicit_module_node_id.startswith("module::"):
            module_name = normalize_module_name(explicit_module_node_id.split("::", 1)[1])

        record = {
            "id": nid,
            "label": label,
            "props": {
                "node_type": node_type,
                "summary": _as_clean_text(raw.get("summary")),
                "details": _as_clean_text(raw.get("details")),
                "importance": int(raw.get("importance", 0) or 0),
                "created": _as_clean_text(raw.get("created")),
                "last_updated": _as_clean_text(raw.get("last_updated")),
                "tags": raw.get("tags", []),
                "project_id": _as_clean_text(raw.get("project_id")) or project_slug,
                "project_slug": project_slug,
                "relative_path": relative_path,
                "source_path": source_path,
                "module_name": module_name,
                "module_node_id": explicit_module_node_id or (build_module_node_id(module_name) if module_name else ""),
                "source_files": source_files,
                "source_modules": source_modules,
                "source_file_node_id": explicit_source_file_node_id or (f"file::{relative_path}" if relative_path else ""),
                "placeholder": bool(raw.get("placeholder", False)),
            },
        }
        node_map[nid] = merge_node_records(node_map.get(nid), record)

    derived_relationships = []
    for node in list(node_map.values()):
        props = node.get("props", {})
        source_file_list = _normalize_text_list(props.get("source_files"))
        source_file_id = _as_clean_text(source_file_list[0]) if source_file_list else ""
        explicit_source_file_node_id = _as_clean_text(props.get("source_file_node_id"))
        module_name = _as_clean_text(props.get("module_name"))
        explicit_module_node_id = _as_clean_text(props.get("module_node_id"))
        module_node_id = explicit_module_node_id or (build_module_node_id(module_name) if module_name else "")
        relative_path = _as_clean_text(props.get("relative_path"))
        if explicit_source_file_node_id:
            source_file_node_id = explicit_source_file_node_id
        elif relative_path:
            source_file_node_id = f"file::{relative_path}"
        elif source_file_id:
            source_file_node_id = f"file::{source_file_id}"
        else:
            source_file_node_id = ""

        if module_node_id:
            module_record = infer_node_shape_from_id(module_node_id, project_slug)
            module_record["placeholder"] = False
            module_props = {k: v for k, v in module_record.items() if k not in {"id", "label"}}
            node_map[module_node_id] = merge_node_records(
                node_map.get(module_node_id),
                {"id": module_node_id, "label": normalize_label(module_record.get("label")), "props": module_props},
            )

        if source_file_node_id:
            file_record = infer_node_shape_from_id(source_file_node_id, project_slug)
            file_record["placeholder"] = False
            if relative_path:
                file_record["relative_path"] = relative_path
                file_record["source_files"] = [relative_path]
                if module_name:
                    file_record["source_modules"] = [module_name]
                    file_record["module_name"] = module_name
                    file_record["module_node_id"] = module_node_id
            file_props = {k: v for k, v in file_record.items() if k not in {"id", "label"}}
            node_map[source_file_node_id] = merge_node_records(
                node_map.get(source_file_node_id),
                {"id": source_file_node_id, "label": normalize_label(file_record.get("label")), "props": file_props},
            )

        if source_file_node_id and module_node_id:
            derived_relationships.append(
                {
                    "source": source_file_node_id,
                    "type": "DEPENDS_ON",
                    "target": module_node_id,
                    "reason": "Document/file belongs to module.",
                    "origin": "document_sync",
                }
            )

    all_relationships = list(relationships or []) + derived_relationships
    for rel in all_relationships:
        source_id = _as_clean_text(rel.get("source"))
        target_id = _as_clean_text(rel.get("target"))
        if not source_id or not target_id:
            continue
        rel_type = normalize_relationship_type(_as_clean_text(rel.get("type")) or "RELATED_TO")
        reason = _as_clean_text(rel.get("reason"))
        origin = _as_clean_text(rel.get("origin")) or "memory_sync"

        if source_id not in node_map:
            inferred = infer_node_shape_from_id(source_id, project_slug)
            inferred_props = {k: v for k, v in inferred.items() if k not in {"id", "label"}}
            node_map[source_id] = {"id": source_id, "label": normalize_label(inferred.get("label")), "props": inferred_props}
        if target_id not in node_map:
            inferred = infer_node_shape_from_id(target_id, project_slug)
            inferred_props = {k: v for k, v in inferred.items() if k not in {"id", "label"}}
            node_map[target_id] = {"id": target_id, "label": normalize_label(inferred.get("label")), "props": inferred_props}

        rel_key = (source_id, rel_type, target_id)
        if rel_key not in rel_seen:
            rel_seen.add(rel_key)
            rel_records.append(
                {
                    "source": source_id,
                    "target": target_id,
                    "type": rel_type,
                    "reason": reason,
                    "origin": origin,
                }
            )
        if rel_type in BIDIRECTIONAL_RELATIONSHIP_TYPES:
            rev_key = (target_id, rel_type, source_id)
            if rev_key not in rel_seen:
                rel_seen.add(rev_key)
                rel_records.append(
                    {
                        "source": target_id,
                        "target": source_id,
                        "type": rel_type,
                        "reason": reason,
                        "origin": origin,
                    }
                )

    node_groups = {}
    for node in node_map.values():
        label = normalize_label(node.get("label", DEFAULT_NODE_LABEL))
        row = {
            "id": node["id"],
            "label": label,
            "props": node.get("props", {}),
        }
        node_groups.setdefault(label, []).append(row)

    rel_groups = {}
    for rel in rel_records:
        rel_groups.setdefault(rel["type"], []).append(
            {
                "source": rel["source"],
                "target": rel["target"],
                "reason": rel.get("reason", ""),
                "origin": rel.get("origin", "memory_sync"),
            }
        )

    return node_groups, rel_groups


def sync_neo4j(all_nodes, relationships, args):
    if args.skip_neo4j:
        return {"enabled": False, "reason": "skip flag"}
    if GraphDatabase is None:
        return {"enabled": False, "reason": "neo4j driver not installed"}
    if not (args.neo4j_password or "").strip():
        return {"enabled": False, "reason": "neo4j-password-missing"}

    manager = Neo4jManager(args.neo4j_uri, args.neo4j_user, args.neo4j_password, args.neo4j_database)
    try:
        project_slug = args.project_slug.lower()
        node_groups, rel_groups = build_neo4j_batches(all_nodes, relationships, project_slug)

        manager.run("CREATE CONSTRAINT memory_node_project_id IF NOT EXISTS FOR (n:MemoryNode) REQUIRE (n.project_slug, n.id) IS UNIQUE")
        manager.run("CREATE INDEX project_slug_idx IF NOT EXISTS FOR (p:Project) ON (p.slug)")
        manager.run("CREATE INDEX memory_node_type_idx IF NOT EXISTS FOR (n:MemoryNode) ON (n.project_slug, n.node_type)")
        manager.run("CREATE INDEX memory_node_path_idx IF NOT EXISTS FOR (n:MemoryNode) ON (n.project_slug, n.relative_path)")
        manager.run("CREATE INDEX memory_node_module_idx IF NOT EXISTS FOR (n:MemoryNode) ON (n.project_slug, n.module_name)")
        manager.run("MERGE (:Project {slug: $project_slug})", project_slug=project_slug)

        nodes_written = 0
        for label, rows in node_groups.items():
            for batch in chunk_rows(rows, 500):
                manager.run(
                    f"""
                    MERGE (p:Project {{slug: $project_slug}})
                    WITH p, $rows AS rows
                    UNWIND rows AS row
                    MERGE (n:MemoryNode {{project_slug: $project_slug, id: row.id}})
                    SET n += row.props, n.node_label = row.label, n.updated_at = datetime()
                    MERGE (p)-[:HAS_NODE]->(n)
                    """,
                    project_slug=project_slug,
                    rows=batch,
                )
                nodes_written += len(batch)

        relationships_written = 0
        for rel_type, rows in rel_groups.items():
            for batch in chunk_rows(rows, 700):
                manager.run(
                    f"""
                    MERGE (p:Project {{slug: $project_slug}})
                    WITH p, $rows AS rows
                    UNWIND rows AS row
                    MERGE (s:MemoryNode {{project_slug: $project_slug, id: row.source}})
                    ON CREATE SET s.node_type='unknown', s.placeholder=true, s.summary='', s.updated_at=datetime()
                    MERGE (t:MemoryNode {{project_slug: $project_slug, id: row.target}})
                    ON CREATE SET t.node_type='unknown', t.placeholder=true, t.summary='', t.updated_at=datetime()
                    MERGE (p)-[:HAS_NODE]->(s)
                    MERGE (p)-[:HAS_NODE]->(t)
                    MERGE (s)-[r:{rel_type}]->(t)
                    SET r.reason=row.reason, r.origin=row.origin, r.updated_at=datetime()
                    """,
                    project_slug=project_slug,
                    rows=batch,
                )
                relationships_written += len(batch)

        return {
            "enabled": True,
            "database": args.neo4j_database,
            "nodes_synced": nodes_written,
            "relationships_synced": relationships_written,
            "node_groups": len(node_groups),
            "relationship_groups": len(rel_groups),
            "batch_mode": "unwind",
        }
    finally:
        manager.close()


def main():
    args = parse_args()
    args.project_slug = validate_project_slug_or_raise(args.project_slug)
    project_pack_root = os.getenv("PROJECT_PACK_ROOT", "").strip()
    if project_pack_root:
        pack_root = Path(project_pack_root).resolve()
        if args.project_slug.lower() not in str(pack_root).lower():
            raise RuntimeError(
                f"PROJECT_PACK_ROOT '{pack_root}' does not match project slug '{args.project_slug}'."
            )
        memory_dir = (pack_root / "memory").resolve()
        relationships_path = (pack_root / "memory" / "relationships.md").resolve()
    else:
        memory_dir = Path(args.memory_dir).resolve()
        relationships_path = Path(args.relationships_path).resolve()
    resolve_neo4j_password_with_fallback(args, memory_dir, relationships_path)
    dep_path = Path(args.dependency_graph_path).resolve() if args.dependency_graph_path else None
    world_path = Path(args.world_model_json_path).resolve() if args.world_model_json_path else None
    ast_graph_path = Path(args.ast_graph_path).resolve() if args.ast_graph_path else None
    task_dag_path = Path(args.task_dag_path).resolve() if args.task_dag_path else None
    task_completions_dir = Path(args.task_completions_dir).resolve() if args.task_completions_dir else None
    project_root = infer_project_root(args.project_root, memory_dir, dep_path, task_dag_path)

    markdown_nodes = [parse_node_file(p, memory_dir, args.project_slug) for p in iter_node_files(memory_dir)] if memory_dir.exists() else []
    intake_nodes, intake_relationships = parse_intake_dependency_graph(dep_path, args.project_slug) if dep_path else ([], [])
    world_nodes, world_relationships = parse_world_model(world_path, args.project_slug) if world_path else ([], [])
    ast_nodes, ast_relationships = parse_ast_graph(ast_graph_path, args.project_slug) if ast_graph_path else ([], [])
    task_nodes, task_relationships = parse_task_trace(task_dag_path, task_completions_dir, args.project_slug) if (task_dag_path or task_completions_dir) else ([], [])
    route_nodes, route_relationships = parse_laravel_route_graph(project_root, args.project_slug) if project_root else ([], [])
    schema_nodes, schema_relationships = parse_database_schema_graph(project_root, args.project_slug) if project_root else ([], [])
    test_nodes, test_relationships = parse_test_observability_graph(project_root, args.project_slug) if project_root else ([], [])
    incident_nodes, incident_relationships = parse_incident_graph(project_root, args.project_slug) if project_root else ([], [])
    requirement_nodes, requirement_relationships = parse_roadmap_business_graph(project_root, args.project_slug) if project_root else ([], [])

    all_nodes = dedupe_nodes(
        markdown_nodes
        + intake_nodes
        + world_nodes
        + ast_nodes
        + task_nodes
        + route_nodes
        + schema_nodes
        + test_nodes
        + incident_nodes
        + requirement_nodes
    )
    all_relationships = collect_relationships(
        markdown_nodes,
        relationships_path,
        intake_relationships
        + world_relationships
        + ast_relationships
        + task_relationships
        + route_relationships
        + schema_relationships
        + test_relationships
        + incident_relationships
        + requirement_relationships,
    )

    qdrant_result = sync_qdrant(all_nodes, args)
    neo4j_result = sync_neo4j(all_nodes, all_relationships, args)

    print(json.dumps({
        "project_slug": args.project_slug,
        "memory_dir": str(memory_dir),
        "memory_dir_exists": memory_dir.exists(),
        "relationships_path": str(relationships_path),
        "dependency_graph_path": str(dep_path) if dep_path else "",
        "world_model_json_path": str(world_path) if world_path else "",
        "ast_graph_path": str(ast_graph_path) if ast_graph_path else "",
        "task_dag_path": str(task_dag_path) if task_dag_path else "",
        "task_completions_dir": str(task_completions_dir) if task_completions_dir else "",
        "project_root": str(project_root) if project_root else "",
        "nodes_discovered": {
            "markdown": len(markdown_nodes),
            "intake_modules": len(intake_nodes),
            "world_entities": len(world_nodes),
            "ast_nodes": len(ast_nodes),
            "task_nodes": len(task_nodes),
            "route_nodes": len(route_nodes),
            "schema_nodes": len(schema_nodes),
            "test_nodes": len(test_nodes),
            "incident_nodes": len(incident_nodes),
            "requirement_nodes": len(requirement_nodes),
            "total": len(all_nodes),
        },
        "relationships_discovered": {
            "intake": len(intake_relationships),
            "world_model": len(world_relationships),
            "ast": len(ast_relationships),
            "task_trace": len(task_relationships),
            "route": len(route_relationships),
            "schema": len(schema_relationships),
            "test": len(test_relationships),
            "incident": len(incident_relationships),
            "requirement": len(requirement_relationships),
            "total": len(all_relationships),
        },
        "qdrant": qdrant_result,
        "neo4j": neo4j_result,
    }, indent=2))


if __name__ == "__main__":
    main()
