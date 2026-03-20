"""
SINC Orchestrator State Rotator v2
Archives project-state.json and large JSONL event files to prevent unbounded growth.

Rotation strategy:
  - project-state.json > SIZE_THRESHOLD_MB → archive to state/archive/YYYY-MM-DD/
  - *.jsonl files > JSONL_THRESHOLD_LINES → rotate keeping last N lines
  - Archives older than RETENTION_DAYS are deleted

Usage:
    python state_rotator.py [--dry-run] [--force]
"""

import argparse
import gzip
import json
import os
import shutil
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE        = Path(__file__).parent.parent.parent
STATE_DIR   = BASE / "state"
ARCHIVE_DIR = STATE_DIR / "archive"
LOGS_DIR    = BASE / "logs"

SIZE_THRESHOLD_MB   = 100      # archive project-state.json if > 100MB
JSONL_THRESHOLD     = 50_000   # rotate .jsonl after 50k lines
JSONL_KEEP_LINES    = 10_000   # keep last 10k lines after rotation
RETENTION_DAYS      = 30       # delete archives older than 30 days

MANAGED_FILES = [
    "project-state.json",
]

MANAGED_JSONL = [
    "loop-step-events.jsonl",
    "tool-usage-log.jsonl",
    "task-events.jsonl",
    "stream-events.jsonl",
    "handoff-log.jsonl",
]


def _now():
    return datetime.now(timezone.utc)


def _log(msg: str):
    ts = _now().isoformat()
    line = f"[{ts}] {msg}"
    print(line)
    try:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOGS_DIR / "state_rotator.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _size_mb(path: Path) -> float:
    try:
        return path.stat().st_size / (1024 * 1024)
    except Exception:
        return 0.0


def archive_large_json(filename: str, dry_run: bool) -> bool:
    source = STATE_DIR / filename
    if not source.exists():
        return False

    size_mb = _size_mb(source)
    if size_mb < SIZE_THRESHOLD_MB:
        _log(f"  SKIP {filename} ({size_mb:.1f}MB < {SIZE_THRESHOLD_MB}MB threshold)")
        return False

    date_str = _now().strftime("%Y-%m-%d")
    ts_str   = _now().strftime("%H%M%S")
    dest_dir = ARCHIVE_DIR / date_str
    dest_gz  = dest_dir / f"{source.stem}-{ts_str}.json.gz"

    _log(f"  ARCHIVE {filename} ({size_mb:.1f}MB) → {dest_gz}")

    if dry_run:
        return True

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Compress to gzip archive
    with open(source, "rb") as f_in, gzip.open(dest_gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    # Write a minimal current state (preserves in-progress tasks)
    try:
        with open(source, encoding="utf-8") as f:
            data = json.load(f)

        minimal = {
            "archived_at":     _now().isoformat(),
            "archive_path":    str(dest_gz),
            "original_size_mb": size_mb,
            "tasks_snapshot":  [
                {k: t.get(k) for k in ["id", "status", "assigned_agent", "priority", "updated_at"]}
                for t in data.get("tasks", [])
                if t.get("status") in {"in-progress", "pending", "blocked-phase-approval"}
            ],
        }
        source.write_text(json.dumps(minimal, indent=2, ensure_ascii=False), encoding="utf-8")
        _log(f"  REPLACED {filename} with minimal snapshot ({len(minimal['tasks_snapshot'])} active tasks)")
    except Exception as e:
        _log(f"  WARN: Could not create minimal snapshot for {filename}: {e}")

    return True


def rotate_jsonl(filename: str, dry_run: bool) -> bool:
    source = STATE_DIR / filename
    if not source.exists():
        return False

    try:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as e:
        _log(f"  ERROR reading {filename}: {e}")
        return False

    if len(lines) < JSONL_THRESHOLD:
        _log(f"  SKIP {filename} ({len(lines)} lines < {JSONL_THRESHOLD} threshold)")
        return False

    date_str = _now().strftime("%Y-%m-%d")
    ts_str   = _now().strftime("%H%M%S")
    dest_dir = ARCHIVE_DIR / date_str
    dest_gz  = dest_dir / f"{source.stem}-{ts_str}.jsonl.gz"
    kept     = lines[-JSONL_KEEP_LINES:]

    _log(f"  ROTATE {filename} ({len(lines)} lines) → archive {len(lines) - len(kept)} lines, keep {len(kept)}")

    if dry_run:
        return True

    dest_dir.mkdir(parents=True, exist_ok=True)

    # Archive full file
    with open(source, "rb") as f_in, gzip.open(dest_gz, "wb") as f_out:
        shutil.copyfileobj(f_in, f_out)

    # Write trimmed file
    source.write_text("\n".join(kept) + "\n", encoding="utf-8")
    _log(f"  TRIMMED {filename} to last {len(kept)} lines")

    return True


def purge_old_archives(dry_run: bool):
    if not ARCHIVE_DIR.exists():
        return
    cutoff = _now() - timedelta(days=RETENTION_DAYS)
    purged = 0
    for date_dir in ARCHIVE_DIR.iterdir():
        if not date_dir.is_dir():
            continue
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if dir_date < cutoff:
            _log(f"  PURGE archive {date_dir} (older than {RETENTION_DAYS} days)")
            if not dry_run:
                shutil.rmtree(date_dir)
            purged += 1
    if purged:
        _log(f"  Purged {purged} old archive directories")


def run_rotator(dry_run: bool = False, force: bool = False):
    _log(f"Starting state rotator (dry_run={dry_run}, force={force})")
    original_threshold = SIZE_THRESHOLD_MB

    if force:
        # Lower threshold to force rotation
        global SIZE_THRESHOLD_MB
        SIZE_THRESHOLD_MB = 0

    rotated = 0

    # Rotate large JSON files
    for filename in MANAGED_FILES:
        if archive_large_json(filename, dry_run):
            rotated += 1

    # Rotate JSONL event files
    for filename in MANAGED_JSONL:
        if rotate_jsonl(filename, dry_run):
            rotated += 1

    # Purge old archives
    purge_old_archives(dry_run)

    SIZE_THRESHOLD_MB = original_threshold
    _log(f"Done. {rotated} files rotated.")
    return rotated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="SINC State Rotator v2")
    parser.add_argument("--dry-run", action="store_true", help="Preview rotations without executing")
    parser.add_argument("--force",   action="store_true", help="Force rotation regardless of size thresholds")
    args = parser.parse_args()
    run_rotator(dry_run=args.dry_run, force=args.force)
