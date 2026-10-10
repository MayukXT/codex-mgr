"""Model controls preserve native routing and use guarded shared saves."""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from rich.cells import cell_len
from rich.console import Console

from codex_mgr.cli import main
from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.adapters import launch
from codex_mgr.core.models import save_model_settings
from codex_mgr.tui import advanced_controls, model_picker
from codex_mgr.utils.paths import get_cmgr_settings_path, get_config_toml_path


def configured(protocol="anthropic_proxy", *, active=True):
    provider = ProviderConfig(
        id="fixture",
        name="Synthetic connection",
        base_url="https://fixture.invalid/v1",
        default_model="exact-model",
        wire_api=protocol,
        auth_type="none",
    )
    settings = CmgrSettings(active_provider_id=provider.id, providers={provider.id: provider})
    if not active:
        settings.providers["other"] = ProviderConfig(
            id="other", name="Other", base_url="https://other.invalid/v1", auth_type="none"
        )
        settings.active_provider_id = "other"
    save_settings(settings)
    native_id = (
        settings.active_provider_id if protocol == "responses" or not active else "native_base"
    )
    base_url = (
        settings.providers[native_id].base_url
        if native_id in settings.providers
        else "https://native.invalid/v1"
    )
    get_config_toml_path().write_text(
        '# keep exact native route\nmodel = "original"\n'
        + f'model_provider = "{native_id}"\n[model_providers.{native_id}]\n'
        + f'name = "Native"\nbase_url = "{base_url}"\nwire_api = "responses"\n',
        encoding="utf-8",
    )
    return settings


@pytest.fixture
def no_adapter_handoff(monkeypatch):
    boundaries = [
        Mock(side_effect=AssertionError("Saving must not launch or read native state."))
        for _ in range(3)
    ]
    for name, boundary in zip(
        ("AdapterRuntime", "read_native_configuration", "launch_codex"), boundaries, strict=True
    ):
        monkeypatch.setattr(launch, name, boundary)
    return boundaries


@pytest.mark.parametrize("protocol", ["chat", "anthropic_proxy"])
@pytest.mark.parametrize("active", [False, True])
def test_cli_and_picker_share_adapter_selection_without_changing_native_files(
    protocol, active, no_adapter_handoff, monkeypatch
):
    configured(protocol, active=active)
    config = get_config_toml_path().read_bytes()
    assert main(["model", "use", "cli-exact", "--provider", "fixture"]) == 0
    assert load_settings().providers["fixture"].default_model == "cli-exact"
    actions = iter(["select", None])
    monkeypatch.setattr(model_picker, "select_menu", lambda *args, **kwargs: next(actions))

    def choose(provider):
        provider.default_model = "tui-exact"
        return True

    monkeypatch.setattr(model_picker, "choose_model", choose)
    model_picker.manage_provider_models("fixture")
    saved = load_settings()
    assert saved.providers["fixture"].default_model == "tui-exact"
    assert saved.active_provider_id == ("fixture" if active else "other")
    assert get_config_toml_path().read_bytes() == config
    assert not get_config_toml_path().with_suffix(".toml.bak").exists()
    assert all(boundary.call_count == 0 for boundary in no_adapter_handoff)


@pytest.mark.parametrize("failure", ["budget", "summary", "mutable-schema", "unselected"])
def test_invalid_adapter_save_preserves_settings_and_native_configuration(
    failure, no_adapter_handoff
):
    configured()
    candidate = load_settings()
    provider = candidate.providers["fixture"]
    if failure == "budget":
        provider.reasoning_budget, provider.reasoning_effort = 100, "medium"
    elif failure == "summary":
        provider.messages_summary = "auto"
    elif failure == "mutable-schema":
        provider.messages_summary = "synthetic-private-invalid"
    else:
        provider.model_selected = False
    before = (get_cmgr_settings_path().read_bytes(), get_config_toml_path().read_bytes())
    ok, message = save_model_settings(candidate, "fixture")
    assert not ok and "synthetic-private-invalid" not in message
    assert (get_cmgr_settings_path().read_bytes(), get_config_toml_path().read_bytes()) == before
    assert all(boundary.call_count == 0 for boundary in no_adapter_handoff)


def test_stale_adapter_save_preserves_external_changes(no_adapter_handoff):
    configured()
    candidate = load_settings()
    changed = load_settings()
    changed.providers["fixture"].name = "External update"
    save_settings(changed)
    candidate.providers["fixture"].max_output_tokens = 4000
    before = get_cmgr_settings_path().read_bytes()
    assert not save_model_settings(candidate, "fixture")[0]
    assert get_cmgr_settings_path().read_bytes() == before


@pytest.mark.parametrize("active", [False, True])
def test_responses_model_save_keeps_guarded_native_activation(active):
    configured("responses", active=active)
    candidate = load_settings()
    candidate.providers["fixture"].default_model = "native-exact"
    before = get_config_toml_path().read_bytes()
    assert save_model_settings(candidate, "fixture")[0]
    after = get_config_toml_path().read_bytes()
    assert (after != before) is active
    if active:
        assert b"native-exact" in after and b'model_provider = "fixture"' in after
    assert load_settings().providers["fixture"].default_model == "native-exact"


@pytest.mark.parametrize(
    "protocol,expected",
    [
        ("responses", ["context", "effort", "save", None]),
        ("chat", ["context", "effort", "output", "streaming", "save", None]),
        (
            "anthropic_proxy",
            ["context", "effort", "output", "streaming", "budget", "summary", "save", None],
        ),
    ],
)
def test_menu_exposes_only_controls_with_transport_effects(protocol, expected, monkeypatch):
    configured(protocol)

    def back(message, choices, **kwargs):
        assert [choice.value for choice in choices] == expected

    monkeypatch.setattr(advanced_controls, "select_menu", back)
    before = get_cmgr_settings_path().read_bytes()
    advanced_controls.run_advanced_controls()
    assert get_cmgr_settings_path().read_bytes() == before


def test_messages_controls_save_exact_values_and_fix_invalid_budget_in_place(
    monkeypatch, no_adapter_handoff
):
    configured()
    actions = iter(
        [
            "output",
            "streaming",
            False,
            "summary",
            "auto",
            "budget",
            "effort",
            "save",
            "budget",
            "save",
        ]
    )
    monkeypatch.setattr(advanced_controls, "select_menu", lambda *args, **kwargs: next(actions))
    values = iter(["4096", "100", "medium", "1024"])
    monkeypatch.setattr(
        advanced_controls.inquirer,
        "text",
        lambda **kwargs: SimpleNamespace(execute=lambda: next(values)),
    )
    failures = Mock()
    monkeypatch.setattr(advanced_controls, "print_error", failures)
    config = get_config_toml_path().read_bytes()
    advanced_controls.run_advanced_controls()
    failures.assert_called_once()
    assert "1024" in failures.call_args.args[0]
    saved = load_settings().providers["fixture"]
    assert saved.max_output_tokens == 4096 and saved.reasoning_budget == 1024
    assert saved.streaming is False and saved.messages_summary == "auto"
    assert saved.reasoning_effort == "medium"
    assert get_config_toml_path().read_bytes() == config


@pytest.mark.parametrize("action", ["context", "output", "budget"])
@pytest.mark.parametrize("value", ["-1", "0", "not-a-number", ""])
def test_numeric_controls_reject_invalid_input_and_back_discards_edits(action, value, monkeypatch):
    configured()
    actions = iter([action, None])
    monkeypatch.setattr(advanced_controls, "select_menu", lambda *args, **kwargs: next(actions))
    monkeypatch.setattr(
        advanced_controls.inquirer, "text", lambda **kwargs: SimpleNamespace(execute=lambda: value)
    )
    error = Mock()
    monkeypatch.setattr(advanced_controls, "print_error", error)
    before = get_cmgr_settings_path().read_bytes()
    advanced_controls.run_advanced_controls()
    assert error.call_count == (0 if value == "" else 1)
    assert get_cmgr_settings_path().read_bytes() == before


@pytest.mark.parametrize("action", ["streaming", "summary"])
def test_submenu_back_preserves_existing_control(action, monkeypatch):
    settings = configured()
    provider = settings.providers["fixture"]
    provider.reasoning_effort, provider.reasoning_budget = "medium", 1024
    provider.messages_summary, provider.streaming = "auto", False
    save_settings(settings)
    actions = iter([action, None, "save"])
    monkeypatch.setattr(advanced_controls, "select_menu", lambda *args, **kwargs: next(actions))
    advanced_controls.run_advanced_controls()
    saved = load_settings().providers["fixture"]
    assert saved.messages_summary == "auto" and saved.streaming is False


@pytest.mark.parametrize(
    "action,field",
    [
        ("context", "context_window_limit"),
        ("output", "max_output_tokens"),
        ("budget", "reasoning_budget"),
    ],
)
def test_blank_numeric_control_clears_the_saved_override(action, field, monkeypatch):
    settings = configured()
    provider = settings.providers["fixture"]
    provider.context_window_limit, provider.max_output_tokens = 12000, 4096
    provider.reasoning_budget, provider.reasoning_effort = 1024, "medium"
    save_settings(settings)
    actions = iter([action, "effort", "save"] if action == "budget" else [action, "save"])
    monkeypatch.setattr(advanced_controls, "select_menu", lambda *args, **kwargs: next(actions))
    monkeypatch.setattr(
        advanced_controls.inquirer, "text", lambda **kwargs: SimpleNamespace(execute=lambda: "")
    )
    advanced_controls.run_advanced_controls()
    saved = load_settings().providers["fixture"]
    assert getattr(saved, field) is None
    if action == "budget":
        assert saved.reasoning_effort is None


@pytest.mark.parametrize("width", [40, 60, 80, 120])
@pytest.mark.parametrize("ascii_mode", [False, True])
def test_control_summary_stays_spaced_and_bounded_with_long_markup_labels(
    width, ascii_mode, monkeypatch
):
    provider = configured().providers["fixture"]
    provider.name = "[red]" + "Very long connection name " * 20
    provider.default_model = "exact-long-model-" * 30
    output = StringIO()
    target = Console(file=output, width=width, color_system=None)
    monkeypatch.setattr(advanced_controls, "console", target)
    monkeypatch.setattr(advanced_controls, "supports_unicode", lambda: not ascii_mode)
    advanced_controls._show_settings(provider)
    rows = output.getvalue().splitlines()
    assert all(cell_len(row) <= width for row in rows)
    assert rows[0] == "" and rows[3] == "" and rows[-1] == ""
    assert rows[1].startswith("[red]") and len(rows) == 11
    assert "Context:" in rows[4] and "Effort:" in rows[5]
    assert "Thinking budget:" in rows[8] and "Summaries:" in rows[9]
    if ascii_mode:
        assert output.getvalue().isascii()
