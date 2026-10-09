"""Scriptable session listing does not require a terminal or wrap JSON."""

import json
from pathlib import Path
from unittest.mock import patch

from codex_mgr.cli import main
from codex_mgr.core.sessions import SessionItem
from codex_mgr.utils.display import console, single_line


def test_session_labels_cannot_inject_terminal_controls():
    assert single_line("title\n\x1b[2J\u202eevil") == "title  [2J evil"


def test_json_output_remains_valid_for_long_titles(capsys):
    item = SessionItem(
        "00000000-0000-0000-0000-000000000001",
        "long title " * 50,
        "",
        "Unknown",
        None,
        "Unknown",
        "/synthetic/workspace",
    )
    with patch("codex_mgr.cli.list_sessions", return_value=[item]) as listing:
        assert main(["sessions", "--json", "--limit", "20", "--offset", "40"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload[0]["title"] == item.thread_name
    assert payload[0]["cwd"] == item.cwd
    assert payload[0]["available"] is False
    listing.assert_called_once_with(
        20, offset=40, search=None, workspace=None, include_archived=False
    )


def test_cli_resume_uses_shared_activation_conversion_service():
    settings = type("Settings", (), {"active_provider_id": "fixture"})()
    item = SessionItem(
        "00000000-0000-0000-0000-000000000001", "Synthetic", "", "Unknown", None, "old"
    )
    with (
        patch("codex_mgr.cli.load_settings", return_value=settings),
        patch("codex_mgr.cli.find_session_by_id_or_title", return_value=item),
        patch("codex_mgr.cli.resume_session", return_value=(0, "closed")) as resume,
    ):
        assert main(["resume", item.id, "--to", "fixture", "--allow-reasoning-loss"]) == 0
    resume.assert_called_once_with(item, "fixture", allow_reasoning_loss=True, settings=settings)


def test_cli_restore_does_not_print_filesystem_exception_paths():
    item = SessionItem(
        "00000000-0000-0000-0000-000000000001",
        "Synthetic",
        "",
        "Unknown",
        Path("synthetic.jsonl"),
        "fixture",
    )
    with (
        patch("codex_mgr.cli.find_session_by_id_or_title", return_value=item),
        patch("codex_mgr.cli.list_session_backups", return_value=[]),
        patch(
            "codex_mgr.cli.restore_session_backup",
            side_effect=OSError("synthetic-sensitive-directory/private-session.jsonl"),
        ),
        console.capture() as captured,
    ):
        assert main(["restore", item.id]) == 1
    assert "synthetic-sensitive-directory" not in captured.get()
