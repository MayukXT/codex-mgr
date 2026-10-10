"""Fresh sessions use validated selected settings before foreground handoff."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from codex_mgr.cli import main
from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core import resume
from codex_mgr.core.adapters import launch as adapter_launch
from codex_mgr.core.native_config import NativeConfiguration
from codex_mgr.core.profiles import ConfigurationError
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


@pytest.fixture
def adapter_boundaries(monkeypatch):
    owner = Mock(base_url="http://127.0.0.1:12345/v1", token="synthetic-owner-token")
    owner.__enter__ = Mock(return_value=owner)
    factory = Mock(return_value=owner)
    monkeypatch.setattr(adapter_launch, "AdapterRuntime", factory)
    monkeypatch.setattr(
        adapter_launch,
        "read_native_configuration",
        Mock(return_value=NativeConfiguration({}, {}, {})),
    )
    pre, post = Mock(), Mock()
    monkeypatch.setattr(adapter_launch, "verify_before_activation", pre)
    monkeypatch.setattr(adapter_launch, "verify_after_activation", post)
    monkeypatch.setattr(resume, "find_codex_binary", lambda: Path("synthetic"))
    runner = Mock(return_value=0)
    monkeypatch.setattr(adapter_launch, "launch_codex", runner)
    return owner, factory, pre, post, runner


def adapter_settings(protocol="chat"):
    settings = configured(wire_api=protocol)
    settings.providers["fixture"].auth_type = "env_var"
    settings.providers["fixture"].auth_token = "$CMGR_SYNTHETIC_START_KEY"
    original = ProviderConfig(
        id="original", name="Original", base_url="https://original.invalid/v1", auth_type="none"
    )
    settings.providers[original.id] = original
    settings.active_provider_id = original.id
    save_settings(settings)
    get_config_toml_path().write_bytes(b'# original native selection\nmodel="original-model"\n')
    return settings


@pytest.mark.parametrize("protocol", ["chat", "anthropic_proxy"])
@pytest.mark.parametrize("status", [0, 7, 130])
def test_fresh_adapter_keeps_original_selection_and_owner_through_handoff(
    protocol, status, adapter_boundaries, monkeypatch
):
    owner, _, _, _, runner = adapter_boundaries
    monkeypatch.setenv("CMGR_SYNTHETIC_START_KEY", "synthetic-upstream-secret")
    settings = adapter_settings(protocol)
    original_config = get_config_toml_path().read_bytes()
    original_settings = get_cmgr_settings_path().read_bytes()

    def handoff(**kwargs):
        owner.close.assert_not_called()
        assert kwargs["prompt"] == "--literal & initial prompt"
        assert get_config_toml_path().read_bytes() == original_config
        saved = json.loads(get_cmgr_settings_path().read_bytes())
        assert saved["active_provider_id"] == "fixture"
        selected = saved["providers"]["fixture"]
        assert selected["base_url"] == "https://fixture.invalid/v1"
        assert selected["wire_api"] == protocol
        assert selected["auth_token"] == "$CMGR_SYNTHETIC_START_KEY"
        assert "CMGR_SYNTHETIC_START_KEY" not in kwargs["environment"]
        assert "synthetic-upstream-secret" not in str(kwargs)
        assert "synthetic-owner-token" not in str(kwargs["extra_args"])
        assert "127.0.0.1" not in str(saved)
        assert "CMGR_ADAPTER_TOKEN_" not in str(saved)
        assert "synthetic-owner-token" not in str(saved)
        return status

    runner.side_effect = handoff
    code, _ = resume.start_session(
        "fixture", prompt="--literal & initial prompt", settings=settings
    )
    assert code == status
    owner.close.assert_called_once()
    assert get_config_toml_path().read_bytes() == original_config
    assert get_cmgr_settings_path().with_suffix(".json.bak").read_bytes() == original_settings
    loaded = load_settings()
    assert loaded.active_provider_id == "fixture"
    assert loaded.providers["fixture"].model_dump() == settings.providers["fixture"].model_dump()


@pytest.mark.parametrize(
    "failure", ["credential", "model", "mutated-url", "controls", "null-prompt", "recovery"]
)
def test_invalid_adapter_never_starts_owner_or_saves_selection(
    failure, adapter_boundaries, monkeypatch
):
    owner, factory, _, _, runner = adapter_boundaries
    monkeypatch.setenv("CMGR_SYNTHETIC_START_KEY", "synthetic-upstream-secret")
    settings = adapter_settings()
    before = get_cmgr_settings_path().read_bytes(), get_config_toml_path().read_bytes()
    provider = settings.providers["fixture"]
    if failure == "credential":
        monkeypatch.delenv("CMGR_SYNTHETIC_START_KEY")
        monkeypatch.setenv("OPENAI_API_KEY", "synthetic-unrelated-key")
    elif failure == "model":
        provider.model_selected = False
    elif failure == "mutated-url":
        provider.base_url = "https://synthetic-secret@fixture.invalid/v1"
    elif failure == "controls":
        provider.reasoning_budget = 1024
    recovery = Mock(return_value=(failure != "recovery", "Synthetic recovery failure."))
    monkeypatch.setattr(resume, "recover_pending_activation", recovery)
    code, message = resume.start_session(
        "fixture", prompt="bad\x00prompt" if failure == "null-prompt" else None, settings=settings
    )
    assert code == 1 and "synthetic-secret" not in message
    assert settings.active_provider_id == "original"
    assert (get_cmgr_settings_path().read_bytes(), get_config_toml_path().read_bytes()) == before
    factory.assert_not_called()
    runner.assert_not_called()
    owner.close.assert_not_called()
    assert recovery.call_count == int(failure == "recovery")


@pytest.mark.parametrize("failure", ["pre", "post", "save", "stale", "launch", "cancel"])
def test_adapter_failure_closes_owner_and_preserves_native_configuration(
    failure, adapter_boundaries, monkeypatch
):
    owner, _, pre, post, runner = adapter_boundaries
    monkeypatch.setenv("CMGR_SYNTHETIC_START_KEY", "synthetic-upstream-secret")
    settings = adapter_settings()
    config = get_config_toml_path().read_bytes()
    before = get_cmgr_settings_path().read_bytes()
    if failure in {"pre", "post"}:
        (pre if failure == "pre" else post).side_effect = ConfigurationError("Synthetic policy.")
    elif failure == "save":
        monkeypatch.setattr(
            "codex_mgr.config.settings.commit_settings",
            Mock(side_effect=PermissionError("synthetic-secret")),
        )
    elif failure == "stale":
        post.side_effect = lambda *_, **__: get_cmgr_settings_path().write_bytes(before + b"\n")
    else:
        runner.side_effect = (
            KeyboardInterrupt() if failure == "cancel" else OSError("synthetic-secret")
        )
    code, message = resume.start_session("fixture", settings=settings)
    assert code == (130 if failure == "cancel" else 1)
    assert "synthetic-secret" not in message
    owner.close.assert_called_once()
    assert get_config_toml_path().read_bytes() == config
    if failure in {"launch", "cancel"}:
        assert settings.active_provider_id == load_settings().active_provider_id == "fixture"
        assert runner.call_count == 1
    else:
        assert settings.active_provider_id == "original"
        assert get_cmgr_settings_path().read_bytes() == before + (
            b"\n" if failure == "stale" else b""
        )
        runner.assert_not_called()


def test_recovery_finishes_before_adapter_native_verification(adapter_boundaries, monkeypatch):
    _, _, pre, _, runner = adapter_boundaries
    monkeypatch.setenv("CMGR_SYNTHETIC_START_KEY", "synthetic-upstream-secret")
    settings = adapter_settings()
    restored = b'model="recovered-model"\n'

    def recover():
        get_config_toml_path().write_bytes(restored)
        return True, "Synthetic recovery finished."

    monkeypatch.setattr(resume, "recover_pending_activation", recover)
    pre.side_effect = lambda *_, **__: (
        get_config_toml_path().read_bytes() == restored
        or pytest.fail("Native verification ran before recovery.")
    )
    assert resume.start_session("fixture", settings=settings)[0] == 0
    assert get_config_toml_path().read_bytes() == restored
    runner.assert_called_once()


@pytest.mark.parametrize("operation", ["preview", "resume"])
def test_adapter_resume_stays_gated_without_any_publication(operation, monkeypatch):
    from codex_mgr.core.sessions import SessionItem

    monkeypatch.setenv("CMGR_SYNTHETIC_START_KEY", "synthetic-upstream-secret")
    monkeypatch.setattr(resume, "find_codex_binary", lambda: Path("synthetic"))
    settings = adapter_settings()
    before = get_cmgr_settings_path().read_bytes(), get_config_toml_path().read_bytes()
    session = SessionItem("00000000-0000-4000-8000-000000000001", "Synthetic", "", "", None, "old")
    if operation == "preview":
        with pytest.raises(ConfigurationError, match="adapter"):
            resume.preview_resume(session, "fixture", settings=settings)
    else:
        assert resume.resume_session(session, "fixture", settings=settings)[0] == 1
    assert (get_cmgr_settings_path().read_bytes(), get_config_toml_path().read_bytes()) == before
