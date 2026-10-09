"""Unit tests for configuration export, import, and sanitization."""

import json
from pathlib import Path
from unittest.mock import patch

from codex_mgr.config.portable import apply_import_bundle, export_bundle, inspect_import_bundle
from codex_mgr.config.schema import CmgrSettings, ProviderConfig


def test_export_and_import_cycle(tmp_path: Path):
    mock_settings_file = tmp_path / "cmgr.json"

    initial_settings = CmgrSettings(
        version=1,
        active_provider_id="openai",
        providers={
            "openai": ProviderConfig(
                id="openai",
                name="OpenAI",
                base_url="https://api.openai.com/v1",
                auth_token="synthetic-private-key",
            )
        },
    )
    mock_settings_file.write_text(
        json.dumps(initial_settings.model_dump(mode="json")), encoding="utf-8"
    )

    # 1. Test sanitized export
    export_file = tmp_path / "bundle.json"
    with patch("codex_mgr.config.settings.get_cmgr_settings_path", return_value=mock_settings_file):
        export_bundle(export_file, sanitize_keys=True)

    bundle_data = json.loads(export_file.read_text(encoding="utf-8"))
    assert bundle_data["is_sanitized"] is True
    # Verify secret key was redacted
    assert bundle_data["providers"]["openai"]["auth_token"] is None

    # 2. Test inspect import
    with patch("codex_mgr.config.settings.get_cmgr_settings_path", return_value=mock_settings_file):
        new_p, conflicts = inspect_import_bundle(export_file)

    assert "openai" in conflicts
    assert len(new_p) == 0

    # 3. Add a new custom provider to bundle and import
    bundle_data["providers"]["custom_provider"] = {
        "id": "custom_provider",
        "name": "Custom Provider",
        "base_url": "https://api.custom.com/v1",
        "wire_api": "chat",
        "auth_type": "bearer_token",
        "default_model": "custom-model",
        "reasoning_effort": "max",
        "models": [],
    }
    export_file.write_text(json.dumps(bundle_data), encoding="utf-8")

    with (
        patch("codex_mgr.config.settings.get_cmgr_settings_path", return_value=mock_settings_file),
        patch("codex_mgr.config.settings.get_backups_dir", return_value=tmp_path / "backups"),
    ):
        added, _updated = apply_import_bundle(export_file, conflict_strategy="merge")

    assert added == 1
    updated_settings = json.loads(mock_settings_file.read_text(encoding="utf-8"))
    assert "custom_provider" in updated_settings["providers"]
