"""Failure and recovery contracts for mutation boundaries."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from codex_mgr.core.backup import create_session_backup, restore_session_backup
from codex_mgr.utils.files import FileConflictError, atomic_write, mutation_lock


def test_external_change_is_preserved(tmp_path):
    target = tmp_path / "config.toml"
    target.write_bytes(b"external edit")
    with pytest.raises(FileConflictError):
        atomic_write(target, b"manager edit", expected=b"original")
    assert target.read_bytes() == b"external edit"
    assert not list(tmp_path.glob("*.tmp"))


def test_failed_replace_preserves_original_and_cleans_temp(tmp_path):
    target = tmp_path / "config.toml"
    target.write_bytes(b"original")
    with (
        patch("codex_mgr.utils.files.os.replace", side_effect=PermissionError),
        pytest.raises(PermissionError),
    ):
        atomic_write(target, b"changed", expected=b"original")
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.tmp"))


def test_overlapping_mutations_fail_without_waiting(tmp_path):
    target = tmp_path / "config.toml"
    with mutation_lock(target), pytest.raises(FileConflictError), mutation_lock(target):
        pytest.fail("overlapping writer entered")
    with mutation_lock(target):
        pass


def test_mutation_lock_excludes_another_process(tmp_path):
    import codex_mgr.utils.files

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(codex_mgr.utils.files.__file__).resolve().parents[2])
    code = """import sys
from pathlib import Path
from codex_mgr.utils.files import mutation_lock, FileConflictError
try:
    with mutation_lock(Path(sys.argv[1])):
        sys.exit(1)
except FileConflictError:
    sys.exit(0)
"""
    target = tmp_path / "config.toml"
    with mutation_lock(target):
        child = subprocess.run(
            [sys.executable, "-c", code, str(target)],
            env=environment,
            capture_output=True,
            timeout=10,
            check=False,
        )
    assert child.returncode == 0


def test_failed_flush_preserves_original(tmp_path):
    target = tmp_path / "config.toml"
    target.write_bytes(b"original")
    with (
        patch("codex_mgr.utils.files.os.fsync", side_effect=OSError("synthetic full disk")),
        pytest.raises(OSError),
    ):
        atomic_write(target, b"changed", expected=b"original")
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.tmp"))


def test_missing_integrity_record_is_not_treated_as_legacy(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(
        b'{"type":"session_meta","payload":{"id":"00000000-0000-0000-0000-000000000001"}}\n'
    )
    with patch("codex_mgr.core.backup.get_backups_dir", return_value=tmp_path / "backups"):
        backup = create_session_backup("00000000-0000-0000-0000-000000000001", rollout)
        backup.with_name(backup.name + ".sha256").unlink()
        with pytest.raises(ValueError, match="integrity"):
            restore_session_backup("00000000-0000-0000-0000-000000000001", rollout, backup)


def test_fast_snapshots_do_not_collide_and_restore_preserves_newer_turns(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    original = (
        json.dumps(
            {"type": "session_meta", "payload": {"id": "00000000-0000-0000-0000-000000000001"}}
        ).encode()
        + b"\n"
    )
    newer = (
        original + b'{"type":"response_item","payload":{"type":"message","content":"new turn"}}\n'
    )
    rollout.write_bytes(original)
    with patch("codex_mgr.core.backup.get_backups_dir", return_value=tmp_path / "backups"):
        first = create_session_backup("00000000-0000-0000-0000-000000000001", rollout)
        second = create_session_backup("00000000-0000-0000-0000-000000000001", rollout)
        assert first != second
        rollout.write_bytes(newer)
        restore_session_backup("00000000-0000-0000-0000-000000000001", rollout, first)
    assert rollout.read_bytes() == original
    snapshots = list(
        (tmp_path / "backups/sessions/00000000-0000-0000-0000-000000000001").glob("*.rollout.jsonl")
    )
    assert any(p.read_bytes() == newer for p in snapshots)


def test_corrupt_backup_is_rejected_before_restore(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(
        b'{"type":"session_meta","payload":{"id":"00000000-0000-0000-0000-000000000001"}}\n'
    )
    with patch("codex_mgr.core.backup.get_backups_dir", return_value=tmp_path / "backups"):
        backup = create_session_backup("00000000-0000-0000-0000-000000000001", rollout)
        backup.write_bytes(b"tampered")
        before = rollout.read_bytes()
        with pytest.raises(ValueError, match="integrity"):
            restore_session_backup("00000000-0000-0000-0000-000000000001", rollout, backup)
    assert rollout.read_bytes() == before


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "a\\b", ".."])
def test_snapshot_paths_cannot_escape_backup_directory(tmp_path, session_id):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"synthetic")
    with pytest.raises(ValueError):
        create_session_backup(session_id, rollout)
