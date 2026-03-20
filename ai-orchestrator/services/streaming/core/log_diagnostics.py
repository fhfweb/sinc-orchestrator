from __future__ import annotations

import re
from datetime import datetime, timezone

_LOG_TIMESTAMP_PATTERNS = (
    re.compile(r"^\[(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\]"),
    re.compile(r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"),
    re.compile(r"(?P<ts>\d{4}-\d{2}-\d{2}[T ][0-9:.]+(?:Z|[+-]\d{2}:\d{2})?)"),
)


def extract_log_timestamp(line: str) -> datetime | None:
    text = str(line or "").strip()
    for pattern in _LOG_TIMESTAMP_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        raw_ts = match.group("ts").replace(" ", "T").replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw_ts)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def log_level_from_line(line: str) -> str:
    upper = str(line or "").upper()
    if any(token in upper for token in ("ERROR", "CRITICAL", "TRACEBACK", "EXCEPTION")):
        return "ERROR"
    if "WARN" in upper:
        return "WARN"
    if "DEBUG" in upper:
        return "DEBUG"
    return "INFO"


def log_fingerprint(line: str, *, limit: int = 180) -> str:
    text = str(line or "").strip()
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ][0-9:.\-+Z]+", "<ts>", text)
    text = re.sub(r"\b[0-9a-f]{8,}\b", "<hex>", text, flags=re.IGNORECASE)
    text = re.sub(r"\bTASK-[A-Za-z0-9_-]+\b", "TASK-<id>", text, flags=re.IGNORECASE)
    text = re.sub(r"\b\d+\b", "<n>", text)
    text = re.sub(r"\s+", " ", text)
    return text[:limit]
