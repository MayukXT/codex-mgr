"""Sensitive export surfaces, import identities and active-state safety."""

import json

import pytest

from codex_mgr.cli import main
from codex_mgr.config.portable import apply_import_bundle, export_bundle
from codex_mgr.config.schema import CmgrSettings, ExportBundle, ModelInfo, ProviderConfig
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.profiles import read_config_toml
from codex_mgr.utils.paths import get_cmgr_settings_path, get_codex_home, get_config_toml_path


def configured():
    provider = ProviderConfig(
        id="example",
        name="Example",
        base_url="https://api.example/v1",
        default_model="exact",
        auth_token="synthetic-private-key",
        http_headers={"Authorization": "synthetic-private-header"},
        env_http_headers={"X-Key": "SELECTED_HEADER_KEY"},
        catalog_scope="synthetic-private-digest",
    )
    save_settings(CmgrSettings(providers={"example": provider}, active_provider_id="example"))
    return provider


def bundle(tmp_path, provider, **kwargs):
    path = tmp_path / "import.json"
    item = ExportBundle(
        exported_at="synthetic-time",
        active_provider_id=provider.id,
        providers={provider.id: provider},
        **kwargs,
    )
    path.write_text(item.model_dump_json())
    return path


def test_export_defaults_clear_tokens_headers_and_scope(tmp_path):
    configured()
    path = export_bundle(tmp_path / "export.json")
    raw = path.read_text()
    assert "synthetic-private" not in raw
    values = json.loads(raw)["providers"]["example"]
    assert values["http_headers"] is None and values["catalog_scope"] is None
    assert values["env_http_headers"] == {"X-Key": "SELECTED_HEADER_KEY"}
    private = export_bundle(tmp_path / "private.json", sanitize_keys=False).read_text()
    assert "synthetic-private-key" in private and "synthetic-private-header" in private


def test_export_preserves_existing_files_and_runtime_settings(tmp_path):
    configured()
    target = tmp_path / "existing.json"
    target.write_text("existing")
    with pytest.raises(ValueError):
        export_bundle(target)
    assert target.read_text() == "existing"
    original = get_cmgr_settings_path().read_bytes()
    with pytest.raises(ValueError):
        export_bundle(get_cmgr_settings_path(), overwrite=True)
    assert get_cmgr_settings_path().read_bytes() == original


@pytest.mark.parametrize(
    "name",
    [
        "cmgr-adapter-continuations.key",
        "cmgr-adapter-continuations.key.sha256",
        "cmgr-adapter-continuations.key.cmgr.lock",
    ],
)
def test_export_cannot_overwrite_reasoning_key_or_its_lock(name):
    configured()
    target = get_codex_home() / name
    target.write_bytes(b"synthetic-existing")
    with pytest.raises(ValueError):
        export_bundle(target, overwrite=True)
    assert target.read_bytes() == b"synthetic-existing"


def test_renamed_imports_keep_independent_connection_ids(tmp_path):
    provider = configured()
    provider.codex_provider_id = "example"
    path = bundle(tmp_path, provider)
    assert apply_import_bundle(path, "rename") == (1, 0)
    assert apply_import_bundle(path, "rename") == (1, 0)
    settings = load_settings()
    assert "example_imported" in settings.providers and "example_imported_2" in settings.providers
    assert settings.providers["example_imported"].codex_provider_id == "example_imported"
    assert settings.active_provider_id == "example"


def test_active_overwrite_applies_config_and_keeps_redacted_credentials(tmp_path):
    provider = configured()
    provider.default_model = "new-model"
    provider.auth_token = None
    provider.http_headers = None
    path = bundle(tmp_path, provider, is_sanitized=True)
    assert apply_import_bundle(path, "overwrite") == (0, 1)
    settings = load_settings()
    assert settings.providers["example"].auth_token == "synthetic-private-key"
    assert read_config_toml()["model"] == "new-model"


def test_failed_active_import_does_not_commit_settings(tmp_path):
    provider = configured()
    original = get_cmgr_settings_path().read_bytes()
    get_config_toml_path().write_text("invalid = [")
    path = bundle(tmp_path, provider)
    with pytest.raises(ValueError):
        apply_import_bundle(path, "overwrite")
    assert get_cmgr_settings_path().read_bytes() == original


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 9},
        {"active_provider_id": "missing"},
        {
            "providers": {
                "wrong-id": {
                    "id": "different",
                    "name": "Example",
                    "base_url": "https://api.example/v1",
                }
            }
        },
    ],
)
def test_invalid_bundle_is_rejected_without_mutation(tmp_path, changes):
    provider = configured()
    path = bundle(tmp_path, provider)
    values = json.loads(path.read_text())
    values.update(changes)
    path.write_text(json.dumps(values))
    original = get_cmgr_settings_path().read_bytes()
    assert main(["import", str(path)]) == 1
    assert get_cmgr_settings_path().read_bytes() == original


def test_merge_counts_updated_providers_and_deduplicates_models(tmp_path):
    provider = configured()
    provider.models = [ModelInfo(id="one", name="One"), ModelInfo(id="one", name="Duplicate")]
    path = bundle(tmp_path, provider)
    assert apply_import_bundle(path, "merge") == (0, 1)
    assert len(load_settings().providers["example"].models) == 1
