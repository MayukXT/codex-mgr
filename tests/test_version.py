"""Unit tests for version checking and update logic."""

from unittest.mock import MagicMock, patch

from codex_mgr.core.version import check_for_updates, get_current_version, perform_self_update


def test_get_current_version():
    v = get_current_version()
    assert v == "1.0.0"


def test_check_for_updates_upstream_newer():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"info": {"version": "1.1.0"}}

    with patch("httpx.Client.get", return_value=mock_resp):
        has_up, latest, msg = check_for_updates()

    assert has_up is True
    assert latest == "1.1.0"
    assert "available" in msg


def test_check_for_updates_up_to_date():
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"info": {"version": "1.0.0"}}

    with patch("httpx.Client.get", return_value=mock_resp):
        has_up, latest, msg = check_for_updates()

    assert has_up is False
    assert latest == "1.0.0"
    assert "up to date" in msg


def test_perform_self_update():
    mock_proc = MagicMock()
    mock_proc.returncode = 0
    mock_proc.stdout = "Successfully installed codex-mgr-1.1.0"
    mock_proc.stderr = ""

    with patch("subprocess.run", return_value=mock_proc):
        ok, msg = perform_self_update()

    assert ok is True
    assert "successfully updated" in msg
