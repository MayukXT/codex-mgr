"""Legacy lookup, selected snapshots and active writer restoration safety."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_mgr.core.backup import (
    create_session_backup,
    list_session_backups,
    restore_session_backup,
)
from codex_mgr.core.session_lock import _native_lock, session_write_guard
from codex_mgr.utils.files import FileConflictError, read_snapshot
from codex_mgr.utils.paths import get_backups_dir, get_codex_home

SID = "00000000-0000-0000-0000-000000000001"


def data(sid=SID, text="original"):
    return (
        json.dumps({"type": "session_meta", "payload": {"id": sid}})
        + "\n"
        + json.dumps({"type": "unknown", "text": text})
        + "\n"
    ).encode()


def test_legacy_suffix_copy_is_identified_and_migrated_without_deletion(tmp_path):
    folder = get_backups_dir() / "sessions" / SID.rsplit("-", 1)[-1]
    folder.mkdir(parents=True)
    legacy = folder / "20260101_000000.rollout.jsonl"
    legacy.write_bytes(data())
    unrelated = folder / "20260102_000000.rollout.jsonl"
    unrelated.write_bytes(data("11111111-0000-0000-0000-000000000001", "different session"))
    assert list_session_backups(SID) == [legacy]
    rollout = tmp_path / "rollout-synthetic.jsonl"
    newer = data(text="newer turn")
    rollout.write_bytes(newer)
    assert restore_session_backup(SID, rollout, legacy) == legacy.resolve()
    assert rollout.read_bytes() == data()
    assert legacy.read_bytes() == data() and unrelated.exists()
    canonical = [path for path in list_session_backups(SID) if path.parent.name == SID]
    assert any(read_snapshot(path) == newer for path in canonical)
    assert any(
        path.name.startswith("legacy_") and read_snapshot(path) == data() for path in canonical
    )


def test_restore_selects_older_snapshot_and_preserves_current_content(tmp_path):
    rollout = tmp_path / "rollout-synthetic.jsonl"
    rollout.write_bytes(data())
    first = create_session_backup(SID, rollout)
    rollout.write_bytes(data(text="second"))
    create_session_backup(SID, rollout)
    rollout.write_bytes(data(text="current"))
    restore_session_backup(SID, rollout, first)
    assert rollout.read_bytes() == data()
    assert any(read_snapshot(path) == data(text="current") for path in list_session_backups(SID))


def test_active_writer_prevents_restore_and_preservation_snapshot(tmp_path):
    rollout = tmp_path / "rollout-synthetic.jsonl"
    rollout.write_bytes(data())
    snapshot = create_session_backup(SID, rollout)
    rollout.write_bytes(data(text="current"))
    folder = get_codex_home() / "thread-writer-locks"
    folder.mkdir(parents=True)
    with _native_lock(folder / f"{SID}.lock", create=True), pytest.raises(FileConflictError):
        restore_session_backup(SID, rollout, snapshot)
    assert rollout.read_bytes() == data(text="current")
    assert len(list_session_backups(SID)) == 1


def test_malformed_legacy_snapshot_and_wrong_identity_are_never_restored(tmp_path):
    rollout = tmp_path / "rollout-synthetic.jsonl"
    rollout.write_bytes(data(text="current"))
    folder = get_backups_dir() / "sessions" / SID
    folder.mkdir(parents=True)
    snapshot = folder / "legacy.rollout.jsonl"
    snapshot.write_bytes(data() + b'{"type":')
    with pytest.raises(ValueError, match="truncated"):
        restore_session_backup(SID, rollout, snapshot)
    snapshot.write_bytes(data("11111111-0000-0000-0000-000000000001"))
    with pytest.raises(ValueError, match="different"):
        restore_session_backup(SID, rollout, snapshot)
    assert rollout.read_bytes() == data(text="current")


def test_missing_file_error_inside_guard_is_not_swallowed(tmp_path):
    rollout = tmp_path / "rollout-synthetic.jsonl"
    rollout.write_bytes(data())
    with (
        pytest.raises(FileNotFoundError, match="synthetic inner error"),
        session_write_guard(SID, rollout),
    ):
        raise FileNotFoundError("synthetic inner error")


def test_native_uuid_lock_is_observed_by_another_process(tmp_path):
    import codex_mgr.core.session_lock

    rollout = tmp_path / "rollout-synthetic.jsonl"
    rollout.write_bytes(data())
    folder = get_codex_home() / "thread-writer-locks"
    folder.mkdir(parents=True)
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(codex_mgr.core.session_lock.__file__).resolve().parents[2])
    code = """import sys
from pathlib import Path
from codex_mgr.core.session_lock import session_write_guard
from codex_mgr.utils.files import FileConflictError
try:
    with session_write_guard(sys.argv[1], Path(sys.argv[2])):
        sys.exit(1)
except FileConflictError:
    sys.exit(0)
"""
    with _native_lock(folder / f"{SID}.lock", create=True):
        result = subprocess.run(
            [sys.executable, "-c", code, SID, str(rollout)],
            env=environment,
            capture_output=True,
            timeout=10,
            check=False,
        )
    assert result.returncode == 0
