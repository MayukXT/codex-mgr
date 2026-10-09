"""Scriptable session listing does not require a terminal or wrap JSON."""

import json
from unittest.mock import patch

from codex_mgr.cli import main
from codex_mgr.core.sessions import SessionItem
from codex_mgr.utils.display import single_line


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
