"""Catalog provenance, request identity races and capability consumers."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import tomlkit
from pydantic import ValidationError

from codex_mgr.config.schema import CmgrSettings, ModelInfo, ProviderConfig
from codex_mgr.core import models
from codex_mgr.core.catalog import catalog_document
from codex_mgr.core.profiles import prepare_configuration
from codex_mgr.tui import advanced_controls


def connection(**values):
    return ProviderConfig(
        id="synthetic", name="Synthetic", base_url="https://api.example/v1", **values
    )


def stamp_catalog(provider):
    provider.catalog_source = (
        "codex:model/list" if provider.auth_type == "official_oauth" else "provider:/models"
    )
    provider.catalog_scope = models.catalog_scope(provider)
    provider.catalog_fetched_at = datetime.now(timezone.utc).isoformat()


def reported_model(**values):
    return ModelInfo(
        id="exact/router:model",
        name="Friendly model",
        context_window=32000,
        supported_reasoning_levels=["low", "high"],
        default_reasoning_effort="high",
        input_modalities=["text", "image"],
        **values,
    )


@pytest.mark.parametrize("field", ["endpoint", "protocol", "authentication", "headers"])
def test_persisted_catalog_follows_selected_connection(monkeypatch, field):
    monkeypatch.setenv("SELECTED_KEY", "synthetic-first")
    item = connection(auth_type="env_var", auth_token="$SELECTED_KEY")
    stamp_catalog(item)
    assert models.catalog_is_fresh(item)
    if field == "endpoint":
        item.base_url = "https://other.example/v1"
    elif field == "protocol":
        item.wire_api = "chat"
    elif field == "authentication":
        item.auth_type = "bearer_token"
    else:
        item.http_headers = {"x-organization": "synthetic-other"}
    assert not models.catalog_is_fresh(item)


def test_catalog_tracks_environment_header_values(monkeypatch):
    monkeypatch.setenv("SELECTED_ORG", "synthetic-first")
    item = connection(env_http_headers={"x-organization": "SELECTED_ORG"})
    stamp_catalog(item)
    assert models.catalog_is_fresh(item)
    monkeypatch.setenv("SELECTED_ORG", "synthetic-second")
    assert not models.catalog_is_fresh(item)


@pytest.mark.parametrize("source", [None, "unverified", "codex:model/list"])
def test_api_catalog_requires_matching_provenance(source):
    item = connection()
    stamp_catalog(item)
    item.catalog_source = source
    assert not models.catalog_is_fresh(item)


@pytest.mark.parametrize("age", [timedelta(hours=2), timedelta(minutes=-1)])
def test_expired_and_future_catalogs_are_unverified(age):
    item = connection()
    stamp_catalog(item)
    item.catalog_fetched_at = (datetime.now(timezone.utc) - age).isoformat()
    assert not models.catalog_is_fresh(item)


def test_unknown_keyring_identity_cannot_certify_persisted_catalog():
    item = connection(auth_type="official_oauth")
    stamp_catalog(item)
    assert item.catalog_scope is None
    assert not models.catalog_is_fresh(item)


@pytest.mark.parametrize("change", ["credential", "endpoint", "protocol", "auth_type"])
def test_discovery_cannot_retag_results_after_connection_changes(monkeypatch, change):
    monkeypatch.setenv("SELECTED_KEY", "synthetic-first")
    item = connection(auth_token="$SELECTED_KEY", models=[reported_model()])
    stamp_catalog(item)
    original = item.model_dump()

    def discover(provider):
        if change == "credential":
            monkeypatch.setenv("SELECTED_KEY", "synthetic-second")
        else:
            setattr(
                provider,
                {"endpoint": "base_url", "protocol": "wire_api", "auth_type": "auth_type"}[change],
                {
                    "endpoint": "https://other.example/v1",
                    "protocol": "chat",
                    "auth_type": "none",
                }[change],
            )
        return True, [ModelInfo(id="other", name="Other")], None

    monkeypatch.setattr(models, "discover_models", discover)
    ok, entries, error = models.refresh_provider_models(item)
    assert not ok and entries == [] and "identity changed" in error
    for field in ("models", "catalog_source", "catalog_scope", "catalog_fetched_at"):
        assert item.model_dump()[field] == original[field]
    assert "synthetic-first" not in error and "synthetic-second" not in error


def test_refresh_preserves_manual_entries_and_overrides(monkeypatch):
    manual = reported_model(is_custom=True)
    remote = ModelInfo(id=manual.id, name="Remote label", context_window=64000)
    item = connection(models=[manual, ModelInfo(id="manual-only", name="Manual", is_custom=True)])
    monkeypatch.setattr(models, "discover_models", lambda provider: (True, [remote], None))
    assert models.refresh_provider_models(item)[0]
    assert models.catalog_is_fresh(item)
    assert item.models[0] == manual
    assert [model.id for model in item.models] == [manual.id, "manual-only"]


def test_stale_capabilities_do_not_change_transport_controls_or_native_catalog():
    item = connection(
        auth_type="none", models=[reported_model(wire_api="chat")], reasoning_effort="custom-effort"
    )
    model = models.select_model(item, "exact/router:model")
    assert len(item.models) == 1
    assert model.id == "exact/router:model" and model.name == "Friendly model"
    assert model.context_window is None and model.input_modalities == []
    assert item.wire_api == "responses" and item.reasoning_effort == "custom-effort"
    configuration = tomlkit.parse(prepare_configuration(item, None).decode())
    assert configuration["model_reasoning_effort"] == "custom-effort"
    exported = catalog_document(item, instructions="Synthetic instruction template")["models"][0]
    assert exported["slug"] == model.id and exported["display_name"] == model.name
    assert exported["context_window"] is None
    assert exported["supported_reasoning_levels"] == []
    assert exported["default_reasoning_level"] is None
    assert exported["input_modalities"] == ["text"]


@pytest.mark.parametrize("manual", [False, True])
def test_fresh_or_manual_metadata_supplies_actual_controls(manual):
    item = connection(auth_type="none", models=[reported_model(is_custom=manual)])
    if not manual:
        stamp_catalog(item)
    models.select_model(item, "exact/router:model")
    assert item.reasoning_effort == "high"
    exported = catalog_document(item, instructions="Synthetic instruction template")["models"][0]
    assert exported["context_window"] == 32000
    assert exported["input_modalities"] == ["text", "image"]
    item.reasoning_effort = "unsupported"
    with pytest.raises(ValueError, match="reasoning"):
        prepare_configuration(item, None)
    item.reasoning_effort = None
    configuration = tomlkit.parse(prepare_configuration(item, None).decode())
    assert "model_reasoning_effort" not in configuration


@pytest.mark.parametrize("bad", ["high\x1b[2J", "high\n", "high\x7f", "high\x9b2J", "  "])
@pytest.mark.parametrize(
    "field", ["default_reasoning_effort", "supported_reasoning_levels", "input_modalities"]
)
def test_control_metadata_rejects_terminal_control_characters(bad, field):
    value = [bad] if field != "default_reasoning_effort" else bad
    with pytest.raises(ValidationError):
        ModelInfo(id="exact", name="Exact", **{field: value})


def test_invalid_remote_control_metadata_is_omitted():
    entries = models._models(
        [
            {"id": "bad", "supportedReasoningEfforts": ["high\x1b[2J"]},
            {"id": "exact", "supportedReasoningEfforts": ["high", "high"]},
        ]
    )
    assert [model.id for model in entries] == ["exact"]
    assert entries[0].supported_reasoning_levels == ["high"]


@pytest.mark.parametrize("selection", ["default", "cancel", "literal-default"])
def test_reasoning_menu_can_restore_default_and_cancel_without_mutating_saved_state(
    monkeypatch, selection
):
    item = connection(
        default_model="exact",
        reasoning_effort="high",
        models=[
            ModelInfo(
                id="exact",
                name="Exact",
                is_custom=True,
                supported_reasoning_levels=["high", "__default__"],
            )
        ],
    )
    settings = CmgrSettings(active_provider_id=item.id, providers={item.id: item})
    monkeypatch.setattr(advanced_controls, "load_settings", lambda: settings)
    actions = iter(["effort", "save"])

    def menu(message, choices, **kwargs):
        if message == "Model settings":
            return next(actions)
        assert kwargs["default"] == "high" and kwargs["allow_back"]
        if selection == "default":
            return choices[0].value
        return None if selection == "cancel" else "__default__"

    monkeypatch.setattr(advanced_controls, "select_menu", menu)
    saved = []
    monkeypatch.setattr(
        advanced_controls,
        "apply_settings",
        lambda candidate, **kwargs: (
            saved.append(candidate.model_copy(deep=True)) is None,
            "Saved",
        ),
    )
    advanced_controls.run_advanced_controls()
    assert item.reasoning_effort == "high"  # The menu edits a separate candidate.
    expected = {"default": None, "cancel": "high", "literal-default": "__default__"}[selection]
    assert saved[0].providers[item.id].reasoning_effort == expected


def test_stale_reasoning_menu_allows_explicit_provider_value(monkeypatch):
    item = connection(default_model="exact/router:model", models=[reported_model()])
    settings = CmgrSettings(active_provider_id=item.id, providers={item.id: item})
    monkeypatch.setattr(advanced_controls, "load_settings", lambda: settings)
    actions = iter(["effort", "save"])
    monkeypatch.setattr(advanced_controls, "select_menu", lambda *args, **kwargs: next(actions))
    monkeypatch.setattr(
        advanced_controls.inquirer,
        "text",
        lambda **kwargs: SimpleNamespace(execute=lambda: "provider-value"),
    )
    saved = []
    monkeypatch.setattr(
        advanced_controls,
        "apply_settings",
        lambda candidate, **kwargs: (
            saved.append(candidate.model_copy(deep=True)) is None,
            "Saved",
        ),
    )
    advanced_controls.run_advanced_controls()
    assert saved[0].providers[item.id].reasoning_effort == "provider-value"
