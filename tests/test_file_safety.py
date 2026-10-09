"""Failure and recovery contracts for mutation boundaries."""

import json
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
    with patch("codex_mgr.utils.files.os.replace", side_effect=PermissionError):
        with pytest.raises(PermissionError):
            atomic_write(target, b"changed", expected=b"original")
    assert target.read_bytes() == b"original"
    assert not list(tmp_path.glob("*.tmp"))


def test_overlapping_mutations_fail_without_waiting(tmp_path):
    target = tmp_path / "config.toml"
    with mutation_lock(target):
        with pytest.raises(FileConflictError):
            with mutation_lock(target):
                pytest.fail("overlapping writer entered")
    with mutation_lock(target):
        pass


def test_fast_snapshots_do_not_collide_and_restore_preserves_newer_turns(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    original = json.dumps({"type": "session_meta", "payload": {"id": "synthetic-session"}}).encode() + b"\n"
    newer = original + b'{"type":"response_item","payload":{"type":"message","content":"new turn"}}\n'
    rollout.write_bytes(original)
    with patch("codex_mgr.core.backup.get_backups_dir", return_value=tmp_path / "backups"):
        first = create_session_backup("synthetic-session", rollout)
        second = create_session_backup("synthetic-session", rollout)
        assert first != second
        rollout.write_bytes(newer)
        restore_session_backup("synthetic-session", rollout, first)
    assert rollout.read_bytes() == original
    snapshots = list((tmp_path / "backups/sessions/synthetic-session").glob("*.rollout.jsonl"))
    assert any(p.read_bytes() == newer for p in snapshots)


def test_corrupt_backup_is_rejected_before_restore(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b'{"type":"session_meta","payload":{"id":"synthetic"}}\n')
    with patch("codex_mgr.core.backup.get_backups_dir", return_value=tmp_path / "backups"):
        backup = create_session_backup("synthetic", rollout)
        backup.write_bytes(b"tampered")
        before = rollout.read_bytes()
        with pytest.raises(ValueError, match="integrity"):
            restore_session_backup("synthetic", rollout, backup)
    assert rollout.read_bytes() == before


@pytest.mark.parametrize("session_id", ["../escape", "a/b", "a\\b", ".."])
def test_snapshot_paths_cannot_escape_backup_directory(tmp_path, session_id):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_bytes(b"synthetic")
    with pytest.raises(ValueError):
        create_session_backup(session_id, rollout)
