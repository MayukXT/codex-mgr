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
from codex_mgr.utils.files import FileConflictError, create_snapshot, read_snapshot
from codex_mgr.utils.paths import get_backups_dir, get_codex_home, get_sessions_dir

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


@pytest.mark.parametrize(
    "metadata",
    [{"history_mode": "paginated"}, {"history_base": {"thread_id": SID, "ordinal": 4}}],
    ids=["paginated", "inherited"],
)
@pytest.mark.parametrize("location", ["current", "snapshot", "both"])
def test_canonical_history_restore_rejects_before_any_replacement_or_snapshot(
    metadata, location, tmp_path
):
    def content(text, canonical):
        records = [json.loads(line) for line in data(text=text).splitlines()]
        if canonical:
            records[0]["payload"].update(metadata)
        return b"".join(json.dumps(record).encode() + b"\n" for record in records)

    rollout = tmp_path / "rollout-synthetic.jsonl"
    rollout.write_bytes(content("original", location != "current"))
    folder = get_backups_dir() / "sessions" / SID
    snapshot = create_snapshot(rollout, folder, suffix=".rollout.jsonl")
    rollout.write_bytes(content("newer turn", location != "snapshot"))
    before = rollout.read_bytes()
    with pytest.raises(ValueError, match="canonical store"):
        restore_session_backup(SID, rollout, snapshot)
    assert rollout.read_bytes() == before
    assert list_session_backups(SID) == [snapshot]
    assert read_snapshot(snapshot) == content("original", location != "current")


@pytest.mark.parametrize("metadata", [{"history_mode": "paginated"}, {"history_base": {}}])
def test_file_only_backup_does_not_claim_to_capture_canonical_history(metadata, tmp_path):
    rollout = tmp_path / "rollout-synthetic.jsonl"
    records = [json.loads(line) for line in data().splitlines()]
    records[0]["payload"].update(metadata)
    rollout.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
    before = rollout.read_bytes()
    with pytest.raises(ValueError, match="canonical store"):
        create_session_backup(SID, rollout)
    assert rollout.read_bytes() == before and not list_session_backups(SID)
    snapshot = create_snapshot(
        rollout, get_backups_dir() / "sessions" / SID, suffix=".rollout.jsonl"
    )
    assert restore_session_backup(SID, rollout, snapshot) == snapshot.resolve()
    assert rollout.read_bytes() == before and list_session_backups(SID) == [snapshot]


@pytest.mark.parametrize("entrypoint", ["cli", "tui"])
def test_canonical_restore_entrypoints_explain_the_store_requirement(entrypoint, monkeypatch):
    from unittest.mock import Mock

    from codex_mgr.cli import main
    from codex_mgr.core.sessions import find_session_by_id_or_title
    from codex_mgr.tui import dashboard
    from codex_mgr.utils.display import console

    folder = get_sessions_dir()
    folder.mkdir(parents=True)
    rollout = folder / f"rollout-synthetic-{SID}.jsonl"
    records = [json.loads(line) for line in data().splitlines()]
    records[0]["payload"]["history_mode"] = "paginated"
    rollout.write_bytes(b"".join(json.dumps(record).encode() + b"\n" for record in records))
    snapshot = create_snapshot(
        rollout, get_backups_dir() / "sessions" / SID, suffix=".rollout.jsonl"
    )
    rollout.write_bytes(rollout.read_bytes() + b'{"type":"unknown","text":"newer turn"}\n')
    before = rollout.read_bytes()
    with console.capture() as captured:
        if entrypoint == "cli":
            assert main(["restore", SID, "--snapshot", snapshot.name]) == 1
        else:
            monkeypatch.setattr(
                dashboard, "pick_session", lambda **_: find_session_by_id_or_title(SID)
            )
            monkeypatch.setattr(dashboard, "pick_snapshot", lambda _: snapshot)
            monkeypatch.setattr(
                dashboard.inquirer, "confirm", lambda **_: Mock(execute=lambda: True)
            )
            monkeypatch.setattr(dashboard.inquirer, "text", lambda **_: Mock(execute=lambda: ""))
            dashboard._handle_revert_session()
    assert "canonical store workflow" in " ".join(captured.get().split())
    assert rollout.read_bytes() == before and list_session_backups(SID) == [snapshot]


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
