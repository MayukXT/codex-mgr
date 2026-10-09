"""Checksummed snapshots and restores that preserve the state being replaced."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from codex_mgr.utils.files import atomic_write, create_snapshot, mutation_lock, read_snapshot
from codex_mgr.utils.paths import get_backups_dir


def _session_directory(session_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", session_id):
        raise ValueError("Invalid session identifier.")
    return get_backups_dir() / "sessions" / session_id


def create_session_backup(session_id: str, rollout_path: Path) -> Path:
    return create_snapshot(rollout_path, _session_directory(session_id), suffix=".rollout.jsonl")


def list_session_backups(session_id: str) -> list[Path]:
    folder = _session_directory(session_id)
    if not folder.exists():
        return []
    return sorted(folder.glob("*.rollout.jsonl"), key=lambda p: p.stat().st_mtime_ns, reverse=True)


def restore_session_backup(
    session_id: str, rollout_path: Path, backup_path: Optional[Path] = None
) -> Path:
    """Restore an identified snapshot after saving the current file separately."""
    folder = _session_directory(session_id).resolve()
    if backup_path is None:
        backups = list_session_backups(session_id)
        if not backups:
            raise FileNotFoundError("No backups found for this session.")
        backup_path = backups[0]
    backup_path = backup_path.resolve()
    if backup_path.parent != folder:
        raise ValueError("Snapshot must belong to this session's backup directory.")
    data = read_snapshot(backup_path)
    try:
        first = next(line for line in data.splitlines() if line.strip())
        record = json.loads(first)
        if record.get("type") != "session_meta" or record.get("payload", {}).get("id") != session_id:
            raise ValueError("Snapshot belongs to a different session.")
    except (StopIteration, json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("Snapshot has no valid session identity.") from exc
    with mutation_lock(rollout_path):
        before = rollout_path.read_bytes()
        if before == data:
            return backup_path
        create_session_backup(session_id, rollout_path)
        atomic_write(rollout_path, data, expected=before)
    return backup_path
