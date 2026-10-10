"""Recovery controls inspect privately, require review and preserve native turns."""

import json
import os
import subprocess
from dataclasses import replace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from test_history_copy import amend_journal, bundle, history

from codex_mgr.cli import main
from codex_mgr.core import history_copy
from codex_mgr.core.history_copy import (
    HistoryCopyLimits,
    list_history_copies,
    prepare_history_copy,
    read_history_copy,
    recover_history_copy,
)
from codex_mgr.core.history_stream import HistoryReadError
from codex_mgr.tui import dashboard, history_copy_view
from codex_mgr.utils.display import console
from codex_mgr.utils.files import FileConflictError
from codex_mgr.utils.paths import get_codex_home


def saved_copy(*, encrypted=False):
    path, records = history()
    if encrypted:
        item = {
            "timestamp": "2026-01-01T00:00:00Z",
            "ordinal": len(records),
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "synthetic-opaque-id",
                "summary": [{"type": "summary_text", "text": "synthetic readable summary"}],
                "encrypted_content": "synthetic-opaque-block",
            },
        }
        records.append(json.dumps(item).encode() + b"\n")
        path.write_bytes(b"".join(records))
    original = path.read_bytes()
    with prepare_history_copy(path, "target") as prepared:
        info = prepared.snapshot()
    return info, path, original


def test_absent_inventory_and_cli_do_not_create_storage(capsys):
    assert list_history_copies() == []
    assert main(["history-copies", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert not get_codex_home().exists()


def test_inventory_is_bounded_paged_and_does_not_read_transcripts(monkeypatch):
    info, _, _ = saved_copy()
    folder = bundle(info.id).parent
    incomplete = str(uuid4())
    (folder / incomplete).mkdir()
    unsafe = str(uuid4())
    (folder / unsafe).write_bytes(b"synthetic private data")
    (folder / ".prepare-synthetic").mkdir()
    monkeypatch.setattr(history_copy, "_bundle", Mock(side_effect=AssertionError("no reads")))
    entries = list_history_copies()
    assert [(item.id, item.status) for item in entries] == sorted(
        [(info.id, "unchecked"), (incomplete, "unchecked"), (unsafe, "unsafe")]
    )
    assert list_history_copies(limit=1, offset=1) == entries[1:2]
    assert list_history_copies(offset=20) == []
    limits = HistoryCopyLimits()
    limits = replace(limits, lineage=replace(limits.lineage, directory_entries=2))
    with pytest.raises(HistoryReadError, match="directory entry limit"):
        list_history_copies(limits=limits)


@pytest.mark.parametrize(
    "parameters", [{"limit": 0}, {"limit": -1}, {"limit": True}, {"offset": -1}, {"offset": True}]
)
def test_inventory_rejects_invalid_page_parameters_without_creating_storage(parameters):
    with pytest.raises(ValueError):
        list_history_copies(**parameters)
    assert not get_codex_home().exists()


def test_inspection_exposes_only_safe_metadata_and_does_not_claim_target_integrity(capsys):
    info, path, original = saved_copy(encrypted=True)
    assert info.provider == "target" and not info.target_present
    assert main(["history-copies", "inspect", info.id, "--json"]) == 0
    output = capsys.readouterr().out
    record = json.loads(output)
    assert record == {
        "id": info.id,
        "original_id": info.original_id,
        "state": "prepared",
        "provider": "target",
        "source_count": 1,
        "encrypted_items_removed": 1,
        "target_present": False,
        "bundle_integrity": "verified",
        "target_integrity": "unchecked",
    }
    assert str(get_codex_home()) not in output + repr(info)
    assert "synthetic readable summary" not in output + repr(info)
    assert "synthetic-opaque" not in output + repr(info)
    assert info.review_digest not in output + repr(info)
    assert path.read_bytes() == original and not info.target.exists()


def test_reviewed_digest_refuses_a_valid_but_changed_journal():
    info, path, original = saved_copy()
    amend_journal(info.id, lambda record: record.update(state="published"))
    assert read_history_copy(info.id).review_digest != info.review_digest
    with pytest.raises(FileConflictError, match="changed after inspection"):
        recover_history_copy(info.id, publish=True, expected_digest=info.review_digest)
    assert path.read_bytes() == original and not info.target.exists()


def test_returned_review_digest_matches_the_settled_journal():
    info, _, _ = saved_copy()
    recovered = recover_history_copy(info.id, publish=True, expected_digest=info.review_digest)
    assert recovered == read_history_copy(info.id)
    assert recover_history_copy(info.id, expected_digest=recovered.review_digest) == recovered


@pytest.mark.skipif(os.name != "nt", reason="Windows junction contract.")
def test_inventory_flags_junction_and_inspection_never_reads_outside_storage(tmp_path):
    info, _, _ = saved_copy()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "journal.json"
    secret.write_bytes(b"synthetic-private-content")
    identity = str(uuid4())
    link = bundle(identity)
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
        capture_output=True,
        check=True,
        timeout=10,
    )
    try:
        assert (
            next(item.status for item in list_history_copies() if item.id == identity) == "unsafe"
        )
        with pytest.raises(HistoryReadError):
            read_history_copy(identity)
        assert secret.read_bytes() == b"synthetic-private-content"
        assert read_history_copy(info.id).state == "prepared"
    finally:
        assert link.parent.resolve().is_relative_to(get_codex_home())
        assert link.resolve() == outside.resolve() and outside.resolve().is_relative_to(tmp_path)
        os.rmdir(link)


@pytest.mark.parametrize("publish", [1, "yes", None])
def test_publication_requires_a_boolean_control(publish):
    info, path, original = saved_copy()
    with pytest.raises(ValueError, match="explicit"):
        recover_history_copy(info.id, publish=publish)
    assert path.read_bytes() == original and not info.target.exists()


def test_cli_explicit_publication_survives_missing_original_and_preserves_later_turns():
    info, path, _ = saved_copy()
    path.unlink()
    assert main(["history-copies", "recover", info.id]) == 0
    assert not info.target.exists()
    assert main(["history-copies", "recover", info.id, "--publish"]) == 0
    published = read_history_copy(info.id)
    assert published.state == "published" and published.target_present
    assert published.review_digest != info.review_digest
    later = b'{"timestamp":"synthetic","type":"event_msg","payload":{}}\n'
    with info.target.open("ab") as stream:
        stream.write(later)
    current = info.target.read_bytes()
    assert main(["history-copies", "recover", info.id, "--publish"]) == 0
    assert info.target.read_bytes() == current
    assert not (get_codex_home() / "config.toml").exists()
    assert not (get_codex_home() / "cmgr.json").exists()


def test_cli_requires_loss_consent_and_keeps_exact_readable_summary(capsys):
    info, path, original = saved_copy(encrypted=True)
    assert main(["history-copies", "recover", info.id, "--publish"]) == 1
    assert not info.target.exists()
    assert "--allow-reasoning-loss" in capsys.readouterr().out
    assert main(["history-copies", "recover", info.id, "--publish", "--allow-reasoning-loss"]) == 0
    assert b"synthetic readable summary" in info.target.read_bytes()
    assert b"synthetic-opaque-block" not in info.target.read_bytes()
    assert path.read_bytes() == original


@pytest.mark.parametrize("failure", ["corrupt-bundle", "changed-target", "duplicate-id"])
def test_cli_recovery_refuses_conflicts_and_does_not_expose_private_contents(failure, capsys):
    info, path, original = saved_copy()
    candidate = bundle(info.id) / "candidate.jsonl"
    if failure == "corrupt-bundle":
        candidate.write_bytes(candidate.read_bytes() + b"synthetic-private-data\n")
    elif failure == "changed-target":
        info.target.parent.mkdir(parents=True, exist_ok=True)
        info.target.write_bytes(b"synthetic-private-data\n")
    else:
        duplicate = get_codex_home() / "archived_sessions" / info.target.name
        duplicate.parent.mkdir()
        duplicate.write_bytes(b"synthetic-private-data\n")
    before = {p: p.read_bytes() for p in get_codex_home().rglob("*") if p.is_file()}
    assert main(["history-copies", "recover", info.id, "--publish"]) == 1
    output = capsys.readouterr().out
    assert str(get_codex_home()) not in output and "synthetic-private-data" not in output
    assert {p: p.read_bytes() for p in before} == before
    assert path.read_bytes() == original


def test_cli_invalid_uuid_suppresses_path_diagnostics(capsys):
    assert main(["history-copies", "inspect", "../synthetic-private-file"]) == 1
    assert "synthetic-private-file" not in capsys.readouterr().out
    assert not get_codex_home().exists()


@pytest.mark.parametrize("consent", [False, True])
def test_tui_publication_is_explicit_and_defaults_to_no(consent, monkeypatch):
    info, path, original = saved_copy(encrypted=True)
    select = Mock(side_effect=[info.id, "publish", None])
    monkeypatch.setattr(history_copy_view, "select_menu", select)
    confirm = Mock()
    confirm.return_value.execute.return_value = consent
    monkeypatch.setattr(history_copy_view.inquirer, "confirm", confirm)
    history_copy_view.manage_history_copies()
    assert info.target.exists() is consent
    assert path.read_bytes() == original
    assert confirm.call_args.kwargs["default"] is False
    assert "1 encrypted items removed" in confirm.call_args.kwargs["message"]


def test_tui_rechecks_the_review_after_confirmation(monkeypatch):
    info, path, original = saved_copy()
    monkeypatch.setattr(
        history_copy_view, "select_menu", Mock(side_effect=[info.id, "publish", None])
    )
    confirm = Mock()

    def change_journal():
        amend_journal(info.id, lambda record: record.update(state="published"))
        return True

    confirm.return_value.execute.side_effect = change_journal
    monkeypatch.setattr(history_copy_view.inquirer, "confirm", confirm)
    history_copy_view.manage_history_copies()
    assert path.read_bytes() == original and not info.target.exists()


def test_tui_settle_does_not_publish_a_missing_target(monkeypatch):
    info, path, original = saved_copy()
    monkeypatch.setattr(
        history_copy_view, "select_menu", Mock(side_effect=[info.id, "settle", None])
    )
    confirm = Mock()
    confirm.return_value.execute.return_value = True
    monkeypatch.setattr(history_copy_view.inquirer, "confirm", confirm)
    history_copy_view.manage_history_copies()
    assert path.read_bytes() == original and not info.target.exists()
    assert read_history_copy(info.id) == info


@pytest.mark.parametrize("width,height", [(40, 12), (60, 20), (80, 24), (120, 40)])
def test_tui_pages_fit_bounded_rows_and_back_creates_no_copy(width, height, monkeypatch):
    info, _, _ = saved_copy()
    for _ in range(12):
        (bundle(info.id).parent / str(uuid4())).mkdir()
    monkeypatch.setattr(console, "width", width)
    monkeypatch.setattr(console, "height", height)
    select = Mock(side_effect=["next", "previous", None])
    monkeypatch.setattr(history_copy_view, "select_menu", select)
    history_copy_view.manage_history_copies()
    size = max(1, min(10, height - 10))
    first = select.call_args_list[0].args[1]
    assert len(first) == size + 2
    assert all(len(choice.name) <= width - 4 for choice in first)
    assert not info.target.exists()


def test_dashboard_history_recovery_remains_reachable_with_malformed_settings(monkeypatch):
    monkeypatch.setattr(dashboard, "load_settings", Mock(side_effect=ValueError))
    monkeypatch.setattr(dashboard, "select_menu", Mock(side_effect=["history", None]))
    action = Mock()
    monkeypatch.setattr(history_copy_view, "manage_history_copies", action)
    dashboard.run_main_dashboard()
    action.assert_called_once_with()
    assert not get_codex_home().exists()
