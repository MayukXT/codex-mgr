"""Checksummed snapshots and restores that preserve the state being replaced."""

from __future__ import annotations

import json
from pathlib import Path

from codex_mgr.core.rollouts import (
    CanonicalHistoryError,
    canonical_session_id,
    metadata_from_record,
    read_session_metadata,
)
from codex_mgr.core.session_lock import session_write_guard
from codex_mgr.utils.files import atomic_write, create_snapshot, read_snapshot
from codex_mgr.utils.json_edits import JsonEdits
from codex_mgr.utils.paths import get_backups_dir


def _session_directory(session_id: str) -> Path:
    return get_backups_dir() / "sessions" / canonical_session_id(session_id)


def create_session_backup(session_id: str, rollout_path: Path) -> Path:
    metadata = read_session_metadata(rollout_path)
    if metadata.id != canonical_session_id(session_id):
        raise ValueError("Rollout belongs to a different session.")
    if metadata.canonical_history:
        raise CanonicalHistoryError()
    return create_snapshot(rollout_path, _session_directory(session_id), suffix=".rollout.jsonl")


def list_session_backups(session_id: str) -> list[Path]:
    session_id = canonical_session_id(session_id)
    folder = _session_directory(session_id)
    backups = []
    for source in (folder, folder.parent / session_id.rsplit("-", 1)[-1]):
        for path in source.glob("*.rollout.jsonl"):
            try:
                if path.resolve().parent != source.resolve():
                    continue
                if source != folder and read_session_metadata(path).id != session_id:
                    continue
                backups.append(path)
            except (OSError, ValueError):
                continue
    return sorted(backups, key=lambda p: p.stat().st_mtime_ns, reverse=True)


def restore_session_backup(
    session_id: str, rollout_path: Path, backup_path: Path | None = None
) -> Path:
    """Restore an identified snapshot after saving the current file separately."""
    session_id = canonical_session_id(session_id)
    folder = _session_directory(session_id).resolve()
    if backup_path is None:
        backups = list_session_backups(session_id)
        if not backups:
            raise FileNotFoundError("No backups found for this session.")
        backup_path = backups[0]
    backup_path = backup_path.resolve()
    legacy = folder.parent / session_id.rsplit("-", 1)[-1]
    if backup_path.parent not in {folder, legacy.resolve()}:
        raise ValueError("Snapshot must belong to this session's backup directory.")
    data = read_snapshot(backup_path)
    try:
        records = 0
        for line in data.splitlines(keepends=True):
            if not line.strip():
                continue
            parsed = JsonEdits(line.decode("utf-8")).value
            if not isinstance(parsed, dict) or not isinstance(parsed.get("type"), str):
                raise TypeError("Snapshot has an invalid rollout record.")
            if records and parsed["type"] == "session_meta":
                raise ValueError("Snapshot has ambiguous session metadata.")
            records += 1
    except (UnicodeDecodeError, ValueError, TypeError, RecursionError) as exc:
        raise ValueError("Snapshot has invalid or truncated rollout records.") from exc
    try:
        first = next(line for line in data.splitlines() if line.strip())
        record = json.loads(first.decode("utf-8-sig"))
        # A suffix directory alone cannot establish the identity of a legacy copy.
        identity_path = rollout_path if backup_path.parent == folder else backup_path
        snapshot_metadata = metadata_from_record(record, identity_path)
        if snapshot_metadata.id != session_id:
            raise ValueError("Snapshot belongs to a different session.")
    except (StopIteration, json.JSONDecodeError, AttributeError) as exc:
        raise ValueError("Snapshot has no valid session identity.") from exc
    with session_write_guard(session_id, rollout_path):
        current_metadata = read_session_metadata(rollout_path)
        if current_metadata.id != session_id:
            raise ValueError("Current rollout belongs to a different session.")
        before = rollout_path.read_bytes()
        if before == data:
            return backup_path
        if current_metadata.canonical_history or snapshot_metadata.canonical_history:
            raise CanonicalHistoryError()
        create_session_backup(session_id, rollout_path)
        if backup_path.parent == legacy.resolve():
            # Preserve the legacy original and create a verified full-UUID copy.
            create_snapshot(backup_path, folder, prefix="legacy_", suffix=".rollout.jsonl")
        atomic_write(rollout_path, data, expected=before)
    return backup_path
