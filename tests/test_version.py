"""Update failures, exact release handoff and subprocess output stay unambiguous."""

import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from codex_mgr import __version__
from codex_mgr.cli import main
from codex_mgr.core import version


def mock_index(monkeypatch, *, body=None, status=200, error=None):
    original = httpx.Client
    requests = []

    def handler(request):
        requests.append(request)
        if error:
            raise error
        return httpx.Response(status, json=body, headers={"location": "https://redirect.invalid"})

    def client(**kwargs):
        assert kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "Client", client)
    return requests


def test_current_version_matches_package():
    assert version.get_current_version() == __version__


@pytest.mark.parametrize(
    "published,update,phrase",
    [
        ("2.0.0", True, "available"),
        ("1.0.0", False, "up to date"),
        ("0.9.0", False, "newer than"),
    ],
)
def test_verified_release_comparison(published, update, phrase, monkeypatch):
    monkeypatch.setattr(version, "__version__", "1.0.0")
    requests = mock_index(monkeypatch, body={"info": {"version": published}})
    result = version.check_for_updates()
    assert result[:2] == (update, published)
    assert phrase in result[2]
    assert len(requests) == 1
    assert requests[0].url == "https://pypi.org/pypi/cmgr-cli/json"


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"info": []},
        {"info": {"version": None}},
        {"info": {"version": "private-invalid-value"}},
        {"info": {"version": "2.0.0rc1"}},
        {"info": {"version": "2.0.0.dev1"}},
        {"info": {"version": "2.0.0+private"}},
        {"info": {"version": "2.0.0", "yanked": True}},
    ],
)
def test_invalid_unstable_or_withdrawn_metadata_is_not_up_to_date(body, monkeypatch):
    mock_index(monkeypatch, body=body)
    available, latest, message = version.check_for_updates()
    assert not available and latest is None
    assert "up to date" not in message
    assert "private" not in message


@pytest.mark.parametrize("status", [301, 401, 404, 429, 500])
def test_http_failure_is_unavailable_not_a_local_build(status, monkeypatch):
    requests = mock_index(monkeypatch, status=status)
    assert version.check_for_updates()[:2] == (False, None)
    assert len(requests) == 1


def test_network_error_does_not_echo_proxy_credentials(monkeypatch):
    mock_index(
        monkeypatch, error=httpx.ConnectError("https://private-user:private-token@proxy.invalid")
    )
    result = version.check_for_updates()
    assert result[:2] == (False, None)
    assert "private" not in result[2]


@pytest.mark.parametrize(
    "candidate", ["private-invalid", "1.0.0", "0.9.0", "2.0rc1", "2.0+local", None]
)
def test_update_rejects_invalid_or_non_upgrade_targets(candidate, monkeypatch):
    monkeypatch.setattr(version, "__version__", "1.0.0")
    run = Mock()
    monkeypatch.setattr(subprocess, "run", run)
    assert version.perform_self_update(candidate)[0] is False
    run.assert_not_called()


def test_update_installs_checked_version_and_verifies_same_interpreter(monkeypatch):
    monkeypatch.setattr(version, "__version__", "1.0.0")
    run = Mock(
        side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout="2.0.0\n")]
    )
    monkeypatch.setattr(subprocess, "run", run)
    ok, message = version.perform_self_update("2.0.0")
    assert ok and "Restart cmgr" in message
    first, second = run.call_args_list
    assert first.args[0] == [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--upgrade",
        "--no-input",
        "cmgr-cli==2.0.0",
    ]
    assert first.kwargs["stdout"] == subprocess.DEVNULL
    assert first.kwargs["stderr"] == subprocess.DEVNULL
    assert second.args[0][0] == sys.executable
    assert "importlib.metadata" in second.args[0][2]


@pytest.mark.parametrize(
    "failure",
    [
        SimpleNamespace(returncode=1, stderr="private-error", stdout="private-index"),
        subprocess.TimeoutExpired("private-command", 45, stderr="private-index"),
        OSError("private-local-path"),
    ],
)
def test_install_failures_are_redacted_and_not_success(failure, monkeypatch):
    run = (
        Mock(side_effect=failure) if isinstance(failure, Exception) else Mock(return_value=failure)
    )
    monkeypatch.setattr(subprocess, "run", run)
    ok, message = version.perform_self_update("2.0.0")
    assert not ok and "private" not in message
    assert run.call_count == 1


@pytest.mark.parametrize("installed", ["1.0.0", "private-invalid", ""])
def test_pip_exit_zero_without_verified_install_is_failure(installed, monkeypatch):
    run = Mock(
        side_effect=[SimpleNamespace(returncode=0), SimpleNamespace(returncode=0, stdout=installed)]
    )
    monkeypatch.setattr(subprocess, "run", run)
    assert version.perform_self_update("2.0.0")[0] is False


@pytest.mark.parametrize("command", [["version"], ["update"], ["update", "--check"]])
def test_unavailable_update_returns_failure_and_never_installs(command, monkeypatch):
    monkeypatch.setattr(version, "check_for_updates", lambda: (False, None, "Unavailable."))
    update = Mock()
    monkeypatch.setattr(version, "perform_self_update", update)
    assert main(command) == 1
    update.assert_not_called()


def test_cli_passes_verified_release_and_propagates_install_failure(monkeypatch):
    monkeypatch.setattr(version, "check_for_updates", lambda: (True, "2.0.0", "Available."))
    update = Mock(return_value=(False, "Failed."))
    monkeypatch.setattr(version, "perform_self_update", update)
    assert main(["update", "--check"]) == 0
    update.assert_not_called()
    assert main(["update"]) == 1
    update.assert_called_once_with("2.0.0")


def test_update_interruption_returns_130(monkeypatch):
    monkeypatch.setattr(version, "check_for_updates", lambda: (True, "2.0.0", "Available."))
    monkeypatch.setattr(version, "perform_self_update", Mock(side_effect=KeyboardInterrupt))
    assert main(["update"]) == 130


@pytest.mark.parametrize("available,latest", [(False, None), (True, "2.0.0")])
def test_tui_unavailable_or_cancelled_update_does_not_install(available, latest, monkeypatch):
    from codex_mgr.tui import dashboard

    monkeypatch.setattr(
        version, "check_for_updates", lambda: (available, latest, "Synthetic status.")
    )
    update = Mock()
    monkeypatch.setattr(version, "perform_self_update", update)
    confirm = Mock(return_value=SimpleNamespace(execute=lambda: False))
    monkeypatch.setattr(dashboard.inquirer, "confirm", confirm)
    monkeypatch.setattr(dashboard.inquirer, "text", lambda **_: SimpleNamespace(execute=lambda: ""))
    dashboard._handle_version_update()
    update.assert_not_called()
    if available:
        assert confirm.call_args.kwargs["default"] is False
    else:
        confirm.assert_not_called()
