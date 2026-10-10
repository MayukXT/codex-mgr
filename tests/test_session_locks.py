"""One native coordinator protects every participating logical thread."""

import os
import subprocess
from uuid import uuid4

import pytest

from codex_mgr.core.session_lock import _native_lock, session_write_guard, session_write_guards
from codex_mgr.utils.files import FileConflictError, mutation_lock
from codex_mgr.utils.paths import get_codex_home


def sources(tmp_path):
    return [(str(uuid4()), tmp_path / f"synthetic-{index}.jsonl") for index in range(3)]


def lock_folder():
    folder = get_codex_home() / "thread-writer-locks"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def test_repeated_thread_ids_and_paths_are_coordinated_once(tmp_path):
    sessions = sources(tmp_path)
    sessions[1] = (sessions[0][0], sessions[1][1])
    with session_write_guards([*sessions, sessions[0], sessions[1]]):
        for _, path in sessions:
            with pytest.raises(FileConflictError), mutation_lock(path):
                pytest.fail("Every source path must be held.")
        with (
            pytest.raises(FileConflictError),
            _native_lock(lock_folder() / ".coordination.lock", create=True),
        ):
            pytest.fail("A new native writer cannot acquire coordination.")
    with session_write_guards(sessions):
        pass
    assert all(not (lock_folder() / f"{identity}.lock").exists() for identity, _ in sessions)


@pytest.mark.parametrize("index", [0, 1, 2])
def test_any_active_logical_source_writer_rejects_the_group(tmp_path, index):
    sessions = sources(tmp_path)
    path = lock_folder() / f"{sessions[index][0]}.lock"
    with (
        _native_lock(path, create=True),
        pytest.raises(FileConflictError),
        session_write_guards(sessions),
    ):
        pytest.fail("An ancestor or root writer must stop publication.")
    with session_write_guards(sessions):
        pass
    assert path.exists()


def test_guard_exception_releases_every_lock_without_deleting_native_files(tmp_path):
    sessions = sources(tmp_path)
    folder = lock_folder()
    for identity, _ in sessions:
        (folder / f"{identity}.lock").write_bytes(b"synthetic-native-file")
    with (
        pytest.raises(RuntimeError, match="synthetic interruption"),
        session_write_guards(sessions),
    ):
        raise RuntimeError("synthetic interruption")
    with session_write_guards(reversed(sessions)):
        pass
    assert all(
        (folder / f"{identity}.lock").read_bytes() == b"synthetic-native-file"
        for identity, _ in sessions
    )


@pytest.mark.parametrize("bad", ["empty", "uuid"])
def test_invalid_groups_create_no_storage_or_manager_locks(tmp_path, bad):
    sessions = (
        [] if bad == "empty" else [("synthetic-invalid-uuid", tmp_path / "nested" / "history")]
    )
    with pytest.raises(ValueError), session_write_guards(sessions):
        pytest.fail("Invalid groups cannot enter.")
    assert not get_codex_home().exists()
    assert not (tmp_path / "nested").exists()


def test_single_session_guard_still_uses_the_shared_coordinator(tmp_path):
    identity, path = sources(tmp_path)[0]
    with (
        session_write_guard(identity, path),
        pytest.raises(FileConflictError),
        session_write_guards(((identity, path),)),
    ):
        pytest.fail("The single-session entry point must hold the same locks.")


def test_non_directory_native_storage_fails_before_source_lock_creation(tmp_path):
    sessions = sources(tmp_path)
    folder = get_codex_home() / "thread-writer-locks"
    folder.parent.mkdir(parents=True)
    folder.write_bytes(b"synthetic-preserved-file")
    with pytest.raises(FileConflictError, match="unsafe"), session_write_guards(sessions):
        pytest.fail("A regular file cannot be native lock storage.")
    assert folder.read_bytes() == b"synthetic-preserved-file"
    assert not list(tmp_path.glob("*.cmgr.lock"))


@pytest.mark.parametrize("component", ["folder", "coordinator", "thread"])
def test_linked_native_lock_storage_is_rejected_before_following(tmp_path, component):
    sessions = sources(tmp_path)
    folder = lock_folder()
    outside = tmp_path / "synthetic-outside"
    outside.mkdir()
    external_file = outside / "external-lock"
    external_file.write_bytes(b"synthetic-preserved")
    target = {
        "folder": folder,
        "coordinator": folder / ".coordination.lock",
        "thread": folder / f"{sessions[0][0]}.lock",
    }[component]
    if component == "folder":
        folder.rmdir()
    try:
        target.symlink_to(
            outside if component == "folder" else external_file,
            target_is_directory=component == "folder",
        )
    except OSError:
        pytest.skip("Symlink creation is unavailable.")
    with pytest.raises(FileConflictError, match="linked or unsafe"), session_write_guards(sessions):
        pytest.fail("Native lock links cannot be followed.")
    assert external_file.read_bytes() == b"synthetic-preserved"
    assert sorted(path.name for path in outside.iterdir()) == ["external-lock"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="POSIX FIFO contract.")
def test_nonregular_native_lock_cannot_block_the_guard(tmp_path):
    sessions = sources(tmp_path)
    path = lock_folder() / ".coordination.lock"
    os.mkfifo(path)
    with pytest.raises(FileConflictError, match="regular file"), session_write_guards(sessions):
        pytest.fail("A FIFO cannot be accepted as a native lock.")


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract.")
def test_windows_native_lock_directory_junction_is_not_followed(tmp_path):
    sessions = sources(tmp_path)
    folder = lock_folder()
    outside = tmp_path / "synthetic-outside-lock-storage"
    outside.mkdir()
    folder.rmdir()
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(folder), str(outside)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    try:
        with pytest.raises(FileConflictError, match="unsafe"), session_write_guards(sessions):
            pytest.fail("A native lock junction must be refused.")
        assert not list(outside.iterdir())
    finally:
        assert folder.parent.resolve().is_relative_to(tmp_path.resolve())
        assert folder.resolve() == outside.resolve()
        os.rmdir(folder)
