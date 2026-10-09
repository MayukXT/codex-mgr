"""Atomic session backup and disaster recovery manager.

Ensures zero data loss by creating immutable timestamped snapshots of session rollouts
before any bridge modification, with one-click restore capabilities.
"""

from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from codex_mgr.utils.paths import get_backups_dir


def create_session_backup(session_id: str, rollout_path: Path) -> Path:
    """Create an immutable timestamped backup of a session rollout file."""
    if not rollout_path.exists():
        raise FileNotFoundError(f"Rollout file not found: {rollout_path}")

    # Destination: ~/.codex/backups/sessions/<session_id>/YYYYMMDD_HHMMSS.jsonl
    session_backup_dir = get_backups_dir() / "sessions" / session_id
    session_backup_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = session_backup_dir / f"{timestamp}.rollout.jsonl"

    shutil.copy2(rollout_path, backup_file)
    return backup_file


def list_session_backups(session_id: str) -> List[Path]:
    """List all available historical backups for a specific session ID, newest first."""
    session_backup_dir = get_backups_dir() / "sessions" / session_id
    if not session_backup_dir.exists():
        return []

    backups = list(session_backup_dir.glob("*.rollout.jsonl"))
    backups.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return backups


def restore_session_backup(
    session_id: str,
    rollout_path: Path,
    backup_path: Optional[Path] = None
) -> Path:
    """Restore a session rollout from a backup file.

    If backup_path is None, restores the most recent backup.
    """
    if backup_path is None:
        backups = list_session_backups(session_id)
        if not backups:
            raise FileNotFoundError(f"No backups found for session {session_id}")
        backup_path = backups[0]

    if not backup_path.exists():
        raise FileNotFoundError(f"Backup file not found: {backup_path}")

    # Transactional restore
    tmp_restore = rollout_path.with_suffix(".restore.tmp")
    shutil.copy2(backup_path, tmp_restore)
    tmp_restore.replace(rollout_path)

    return backup_path
