"""Settings management and persistence for codex-mgr (cmgr.json).

Guarantees atomic saving, automatic timestamped backups, and seamless initial
onboarding by detecting existing active configurations.
"""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.core.providers import get_default_presets
from codex_mgr.utils.paths import (
    get_backups_dir,
    get_cmgr_settings_path,
    get_config_toml_path,
)


def load_settings() -> CmgrSettings:
    """Load settings from ~/.codex/cmgr.json or initialize defaults on first run."""
    settings_path = get_cmgr_settings_path()

    if not settings_path.exists():
        # First time initialization
        settings = _init_default_settings()
        save_settings(settings, create_backup=False)
        return settings

    try:
        content = settings_path.read_text(encoding="utf-8")
        settings_dict = json.loads(content)
        settings = CmgrSettings.model_validate(settings_dict)

        # Merge any newly added default presets without overwriting user custom edits
        default_presets = get_default_presets()
        modified = False
        for pid, preset in default_presets.items():
            if pid not in settings.providers:
                settings.providers[pid] = preset
                modified = True

        if modified:
            save_settings(settings, create_backup=False)

        return settings
    except Exception:
        # If parsing fails, try to restore from .bak
        bak_path = settings_path.with_suffix(".json.bak")
        if bak_path.exists():
            try:
                content = bak_path.read_text(encoding="utf-8")
                return CmgrSettings.model_validate_json(content)
            except Exception:
                pass
        # Fallback to pristine defaults
        return _init_default_settings()


def save_settings(settings: CmgrSettings, create_backup: bool = True) -> None:
    """Atomically save settings to ~/.codex/cmgr.json with backup snapshotting."""
    settings_path = get_cmgr_settings_path()
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Snapshot backup if file exists
    if settings_path.exists() and create_backup and settings.preferences.auto_backup:
        try:
            # Immediate .bak copy
            bak_path = settings_path.with_suffix(".json.bak")
            shutil.copy2(settings_path, bak_path)

            # Timestamped snapshot in ~/.codex/backups/settings/
            snap_dir = get_backups_dir() / "settings"
            snap_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            snap_file = snap_dir / f"cmgr_{ts}.json"
            shutil.copy2(settings_path, snap_file)
        except Exception:
            pass  # Do not block save if snapshot directory is locked

    # 2. Write to temp file and atomically replace
    tmp_path = settings_path.with_suffix(".json.tmp")
    data = settings.model_dump(mode="json")
    formatted_json = json.dumps(data, indent=2, ensure_ascii=False)

    tmp_path.write_text(formatted_json, encoding="utf-8")
    tmp_path.replace(settings_path)


def _init_default_settings() -> CmgrSettings:
    """Initialize fresh settings, detecting existing active provider in config.toml."""
    presets = get_default_presets()
    active_id = "openai"

    config_path = get_config_toml_path()
    if config_path.exists():
        try:
            text = config_path.read_text(encoding="utf-8")
            for line in text.splitlines():
                stripped = line.strip()
                if stripped.startswith("model_provider") and "=" in stripped:
                    val = stripped.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        active_id = val
                        break
        except Exception:
            pass

    return CmgrSettings(
        version=1,
        active_provider_id=active_id if active_id in presets else "openai",
        providers=presets,
    )
