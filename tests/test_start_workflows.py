"""Fresh sessions use validated selected settings before foreground handoff."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from codex_mgr.cli import main
from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.core import resume
from codex_mgr.tui import dashboard
from codex_mgr.utils.paths import get_cmgr_settings_path, get_config_toml_path


def configured(**values):
    provider = ProviderConfig(
        id="fixture",
        codex_provider_id="native_fixture",
        name="Synthetic connection",
        base_url="https://fixture.invalid/v1",
        default_model="exact-fixture-model",
        auth_type="none",
        **values,
    )
    return CmgrSettings(active_provider_id=provider.id, providers={provider.id: provider})


def test_fresh_session_applies_actual_connection_and_model_before_launch(monkeypatch):
    settings = configured()
    monkeypatch.setattr(resume, "find_codex_binary", lambda: Path("synthetic"))

    def launch(*, prompt):
        assert prompt == "initial exact prompt"
        config = get_config_toml_path().read_text(encoding="utf-8")
        assert 'model_provider = "native_fixture"' in config
        assert 'model = "exact-fixture-model"' in config
        assert json.loads(get_cmgr_settings_path().read_bytes())["active_provider_id"] == "fixture"
        return 0

    handoff = Mock(side_effect=launch)
    monkeypatch.setattr(resume, "launch_codex", handoff)
    assert resume.start_session(prompt="initial exact prompt", settings=settings)[0] == 0
    handoff.assert_called_once_with(prompt="initial exact prompt")


@pytest.mark.parametrize(
    "failure", ["unknown", "missing-credential", "unselected", "missing-codex"]
)
def test_invalid_target_has_no_side_effects_or_launch(failure, monkeypatch):
    settings = configured()
    provider = settings.providers["fixture"]
    if failure == "missing-credential":
        provider.auth_type = "env_var"
        provider.auth_token = "$CMGR_MISSING_START_KEY"
        monkeypatch.delenv("CMGR_MISSING_START_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "unrelated-synthetic")
    if failure == "unselected":
        provider.model_selected = False
    monkeypatch.setattr(
        resume,
        "find_codex_binary",
        lambda: None if failure == "missing-codex" else Path("synthetic"),
    )
    handoff = Mock()
    monkeypatch.setattr(resume, "launch_codex", handoff)
    target = "unknown" if failure == "unknown" else "fixture"
    assert resume.start_session(target, settings=settings)[0] == 1
    handoff.assert_not_called()
    assert not get_cmgr_settings_path().exists()
    assert not get_config_toml_path().exists()


def test_failed_activation_never_starts_fresh_codex(monkeypatch):
    settings = configured()
    monkeypatch.setattr(resume, "find_codex_binary", lambda: Path("synthetic"))
    monkeypatch.setattr(
        resume, "apply_settings", lambda *_, **__: (False, "Synthetic activation failure.")
    )
    handoff = Mock()
    monkeypatch.setattr(resume, "launch_codex", handoff)
    assert resume.start_session(settings=settings) == (1, "Synthetic activation failure.")
    handoff.assert_not_called()


def test_fresh_session_preserves_native_error_status(monkeypatch):
    monkeypatch.setattr(resume, "find_codex_binary", lambda: Path("synthetic"))
    monkeypatch.setattr(resume, "launch_codex", lambda **_: 130)
    code, message = resume.start_session(settings=configured())
    assert code == 130 and "unsuccessfully" in message


def test_cli_start_passes_prompt_and_selected_connection(monkeypatch):
    start = Mock(return_value=(7, "Synthetic native error."))
    monkeypatch.setattr("codex_mgr.cli.start_session", start)
    assert main(["start", "literal initial prompt", "--provider", "fixture"]) == 7
    start.assert_called_once_with("fixture", prompt="literal initial prompt")


def test_tui_new_session_uses_shared_service(monkeypatch):
    settings = configured()
    actions = iter(["new", "exit"])
    monkeypatch.setattr(dashboard, "load_settings", lambda: settings)
    monkeypatch.setattr(dashboard, "select_menu", lambda **_: next(actions))
    monkeypatch.setattr(dashboard, "print_banner", lambda **_: None)
    start = Mock(return_value=(0, "Codex closed."))
    monkeypatch.setattr(dashboard, "start_session", start)
    dashboard.run_main_dashboard()
    start.assert_called_once_with(settings=settings)


def test_header_only_auth_is_resolved_before_fresh_session(monkeypatch):
    settings = configured(env_http_headers={"X-API-Key": "CMGR_SELECTED_HEADER"})
    settings.providers["fixture"].auth_type = "bearer_token"
    monkeypatch.setattr(resume, "find_codex_binary", lambda: Path("synthetic"))
    monkeypatch.delenv("CMGR_SELECTED_HEADER", raising=False)
    handoff = Mock(return_value=0)
    monkeypatch.setattr(resume, "launch_codex", handoff)
    assert resume.start_session(settings=settings)[0] == 1
    handoff.assert_not_called()
    assert not get_config_toml_path().exists()
    monkeypatch.setenv("CMGR_SELECTED_HEADER", "synthetic-secret")
    assert resume.start_session(settings=settings)[0] == 0
    assert "CMGR_SELECTED_HEADER" in get_config_toml_path().read_text(encoding="utf-8")


def test_null_prompt_is_rejected_before_configuration_changes(monkeypatch):
    handoff = Mock()
    monkeypatch.setattr(resume, "launch_codex", handoff)
    assert resume.start_session(prompt="invalid\x00prompt", settings=configured())[0] == 1
    handoff.assert_not_called()
    assert not get_config_toml_path().exists()
