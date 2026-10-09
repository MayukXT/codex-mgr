"""CLI and setup flows use the same guarded provider/model services."""

from types import SimpleNamespace

import pytest

from codex_mgr.cli import main
from codex_mgr.config.schema import ProviderConfig
from codex_mgr.config.settings import load_settings
from codex_mgr.core.auth import LoginStatus
from codex_mgr.core.catalog import catalog_document, export_codex_catalog
from codex_mgr.core.providers import store_provider
from codex_mgr.tui import provider_wizard as wizard


def test_cli_model_and_provider_management_preserves_active_configuration():
    settings = load_settings()
    item = ProviderConfig(
        id="custom",
        name="Custom",
        base_url="https://api.example/v1",
        default_model="first",
        auth_type="none",
    )
    assert store_provider(settings, item)[0]
    assert main(["model", "add", "other", "--provider", "custom", "--name", "Other"]) == 0
    assert main(["model", "use", "other", "--provider", "custom"]) == 0
    assert load_settings().providers["custom"].default_model == "other"
    assert main(["model", "remove", "other", "--provider", "custom"]) == 1
    assert main(["model", "remove", "first", "--provider", "custom"]) == 1  # not registered
    assert main(["provider", "delete", "custom"]) == 0
    assert "custom" not in load_settings().providers


@pytest.mark.parametrize("template", ["openai_api", "anthropic"])
def test_setup_separates_api_key_templates_and_saves_exact_manual_model(monkeypatch, template):
    selections = iter(
        [template, "responses" if template == "openai_api" else "anthropic_proxy", "env"]
    )
    monkeypatch.setattr(wizard, "select_menu", lambda *args, **kwargs: next(selections))
    words = iter(
        [template, "Connection", "https://api.example/v1", "SELECTED_KEY", "exact-provider-model"]
    )
    monkeypatch.setattr(
        wizard.inquirer, "text", lambda **kwargs: SimpleNamespace(execute=lambda: next(words))
    )
    answers = iter([False, False])
    monkeypatch.setattr(
        wizard.inquirer, "confirm", lambda **kwargs: SimpleNamespace(execute=lambda: next(answers))
    )
    monkeypatch.setattr(
        wizard.inquirer, "fuzzy", lambda **kwargs: SimpleNamespace(execute=lambda: "__manual__")
    )
    item = wizard.run_provider_wizard()
    assert item is not None
    assert item.auth_type == "env_var" and item.auth_token == "$SELECTED_KEY"
    assert item.default_model == "exact-provider-model" and item.models[0].context_window is None
    assert load_settings().providers[template].default_model == "exact-provider-model"


def test_oauth_setup_does_not_request_or_probe_api_key(monkeypatch):
    monkeypatch.setattr(wizard, "select_menu", lambda *args, **kwargs: "openai")
    words = iter(["openai", "ChatGPT", "exact-oauth-model"])
    monkeypatch.setattr(
        wizard.inquirer, "text", lambda **kwargs: SimpleNamespace(execute=lambda: next(words))
    )
    monkeypatch.setattr(
        wizard, "codex_login_status", lambda: LoginStatus(True, "chatgpt", "Signed in")
    )
    answers = iter([False, True])
    monkeypatch.setattr(
        wizard.inquirer, "confirm", lambda **kwargs: SimpleNamespace(execute=lambda: next(answers))
    )
    monkeypatch.setattr(
        wizard.inquirer, "fuzzy", lambda **kwargs: SimpleNamespace(execute=lambda: "__manual__")
    )
    item = wizard.run_provider_wizard()
    assert item and item.auth_type == "official_oauth" and item.auth_token is None
    assert load_settings().providers["openai"].default_model == "exact-oauth-model"


def test_codex_catalog_preserves_labels_and_unknown_capabilities(tmp_path):
    item = ProviderConfig(
        id="custom",
        name="Custom",
        base_url="https://api.example/v1",
        default_model="exact",
        auth_token="synthetic-private",
    )
    from codex_mgr.core.models import select_model

    select_model(item, "exact")
    item.models[0].name = "Friendly model"
    document = catalog_document(item, instructions="Synthetic instruction template")
    assert document["models"][0]["slug"] == "exact"
    assert document["models"][0]["display_name"] == "Friendly model"
    assert document["models"][0]["context_window"] is None
    assert document["models"][0]["supported_reasoning_levels"] == []
    path = export_codex_catalog(
        item, tmp_path / "catalog.json", instructions="Synthetic instruction template"
    )
    assert "synthetic-private" not in path.read_text()
    with pytest.raises(ValueError):
        export_codex_catalog(item, path, instructions="Synthetic instruction template")


def test_noninteractive_menu_and_setup_fail_without_prompting(monkeypatch):
    import sys

    monkeypatch.setattr(sys, "stdin", SimpleNamespace(isatty=lambda: False))
    assert main([]) == 1
    assert main(["provider", "add"]) == 1
    assert main(["resume"]) == 1


def test_custom_catalog_rejects_empty_instruction_template():
    item = ProviderConfig(id="custom", name="Custom", base_url="https://api.example/v1")
    with pytest.raises(ValueError, match="instruction template"):
        catalog_document(item, instructions="")
