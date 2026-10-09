"""Session identity, completeness and ambiguity contracts."""

import json
import os
from pathlib import Path
from uuid import UUID

import pytest

from codex_mgr.core.rollouts import SessionIdentityError, read_session_metadata
from codex_mgr.core.sessions import (
    AmbiguousSessionError,
    discover_sessions,
    find_session_by_id_or_title,
    list_sessions,
)
from codex_mgr.utils.paths import get_codex_home, get_session_index_path, get_sessions_dir


def sid(number):
    return str(UUID(int=number))


def rollout(number, *, folder=None, name=None, metadata=None):
    folder = folder or get_sessions_dir()
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (name or f"rollout-2026-10-10T12-00-00-{sid(number)}.jsonl")
    payload = {"id": sid(number), "model_provider": "fixture", "cwd": str(Path.cwd())}
    payload.update(metadata or {})
    path.write_text(
        json.dumps({"type": "session_meta", "payload": payload}) + "\n", encoding="utf-8"
    )
    return path


def index(*entries):
    get_codex_home().mkdir(parents=True, exist_ok=True)
    get_session_index_path().write_text(
        "\n".join(json.dumps(entry) for entry in entries) + "\n", encoding="utf-8"
    )


def test_full_uuid_and_strict_filename_fallback():
    path = rollout(1)
    assert read_session_metadata(path).id == sid(1)
    path = rollout(2, metadata={"id": None})
    assert read_session_metadata(path).id == sid(2)
    assert read_session_metadata(path).from_filename
    for value in ["000000000002", "../escape", "", 3]:
        path = rollout(2, metadata={"id": value})
        with pytest.raises(SessionIdentityError):
            read_session_metadata(path)


def test_identity_conflict_is_not_indexed_as_a_different_session():
    path = rollout(1, metadata={"id": sid(2)})
    with pytest.raises(SessionIdentityError, match="different"):
        read_session_metadata(path)
    catalog = discover_sessions()
    assert not catalog.sessions
    assert catalog.skipped_rollouts == 1


def test_header_can_have_blank_lines_and_bom_but_not_nonmetadata():
    path = rollout(1)
    path.write_bytes(b"\n\r\n\xef\xbb\xbf" + path.read_bytes())
    assert read_session_metadata(path).id == sid(1)
    path.write_text('{"type":"response_item","payload":{}}\n')
    with pytest.raises(SessionIdentityError):
        read_session_metadata(path)


def test_unindexed_history_is_unbounded_and_pages_have_no_duplicates():
    for number in range(1, 132):
        path = rollout(number)
        os.utime(path, (number, number))
    items = list_sessions()
    assert len(items) == 131
    assert items[0].id == sid(131)
    assert find_session_by_id_or_title(sid(1)).id == sid(1)
    pages = list_sessions(50) + list_sessions(50, offset=50) + list_sessions(50, offset=100)
    assert [item.id for item in pages] == [item.id for item in items]


def test_latest_index_title_deduplicates_and_missing_rollout_stays_visible():
    rollout(1)
    index(
        {"id": sid(1), "thread_name": "new title", "updated_at": "2026-10-10T13:00:00Z"},
        {"id": sid(1), "thread_name": "old title", "updated_at": "2026-10-09T13:00:00Z"},
        {"id": sid(2), "thread_name": "missing", "updated_at": "2026-10-11T13:00:00Z"},
    )
    catalog = discover_sessions()
    assert len(catalog.sessions) == 2
    assert find_session_by_id_or_title(sid(1)).thread_name == "new title"
    with pytest.raises(FileNotFoundError):
        find_session_by_id_or_title(sid(2)).require_rollout()


def test_archived_and_workspace_filters_are_explicit(tmp_path):
    rollout(1, metadata={"cwd": str(tmp_path / "first")})
    rollout(2, metadata={"cwd": str(tmp_path / "second")})
    rollout(
        3, folder=get_codex_home() / "archived_sessions", metadata={"cwd": str(tmp_path / "first")}
    )
    assert len(list_sessions()) == 2
    filtered = list_sessions(workspace=tmp_path / "first", include_archived=True)
    assert {item.id for item in filtered} == {sid(1), sid(3)}
    assert next(item for item in filtered if item.id == sid(3)).archived


def test_ambiguous_title_and_uuid_prefix_do_not_choose_first():
    rollout(1)
    rollout(2)
    index(
        {"id": sid(1), "thread_name": "shared title"}, {"id": sid(2), "thread_name": "shared title"}
    )
    for query in ["shared", "00000000"]:
        with pytest.raises(AmbiguousSessionError, match="2 sessions"):
            find_session_by_id_or_title(query)
    assert find_session_by_id_or_title(sid(1)).id == sid(1)


def test_duplicate_rollout_paths_cannot_be_mutated():
    rollout(1)
    rollout(1, folder=get_sessions_dir() / "duplicate")
    items = list_sessions()
    assert len(items) == 1
    assert len(items[0].duplicate_paths) == 2
    with pytest.raises(AmbiguousSessionError, match="Multiple rollout"):
        items[0].require_rollout()


def test_corrupt_index_does_not_hide_readable_history():
    rollout(1)
    get_session_index_path().write_text('garbage\n[]\n{"id":"suffix"}\n', encoding="utf-8")
    catalog = discover_sessions()
    assert len(catalog.sessions) == 1
    assert catalog.skipped_index_records == 3


def test_search_and_invalid_page_bounds():
    rollout(1)
    index({"id": sid(1), "thread_name": "Useful title"})
    assert list_sessions(search="USEFUL")[0].id == sid(1)
    assert list_sessions(search="absent") == []
    assert list_sessions(0) == []
    with pytest.raises(ValueError):
        list_sessions(offset=-1)
