"""Cross-platform path resolution for Codex CLI and codex-mgr.

Guarantees 100% dynamic discovery with zero hardcoded user paths or machine identities.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path
from typing import Optional


def get_codex_home() -> Path:
    """Return the active CODEX_HOME directory.

    Resolves from $CODEX_HOME environment variable if set, otherwise defaults to ~/.codex.
    """
    env_home = os.environ.get("CODEX_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def get_cmgr_settings_path() -> Path:
    """Return the path to the codex-mgr settings file (cmgr.json)."""
    return get_codex_home() / "cmgr.json"


def get_config_toml_path() -> Path:
    """Return the path to Codex's primary config.toml."""
    return get_codex_home() / "config.toml"


def get_sessions_dir() -> Path:
    """Return the path to the Codex sessions directory."""
    return get_codex_home() / "sessions"


def get_session_index_path() -> Path:
    """Return the path to session_index.jsonl."""
    return get_codex_home() / "session_index.jsonl"


def get_models_cache_path() -> Path:
    """Return the path to models_cache.json if present."""
    return get_codex_home() / "models_cache.json"


def get_backups_dir() -> Path:
    """Return the base backups directory (~/.codex/backups)."""
    backups_dir = get_codex_home() / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    return backups_dir


def get_auth_json_path() -> Path:
    """Return the path to ~/.codex/auth.json."""
    return get_codex_home() / "auth.json"


def find_codex_binary() -> Optional[Path]:
    """Dynamically locate the codex executable on the system.

    Searches:
    1. PATH environment variable
    2. Windows %LOCALAPPDATA%\\OpenAI\\Codex\\bin\\**\\codex.exe (newest binary)
    3. Standard Linux / macOS installation paths
    """
    # 1. Search PATH
    which_cmd = shutil.which("codex")
    if which_cmd:
        resolved = Path(which_cmd).resolve()
        if resolved.exists():
            return resolved

    # 2. Windows LocalAppData search
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            base_bin_dir = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
            if base_bin_dir.exists():
                # Scan subdirectories for codex.exe and pick the newest by mtime
                candidates = list(base_bin_dir.glob("*/codex.exe"))
                if candidates:
                    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
                    return candidates[0]
                direct_exe = base_bin_dir / "codex.exe"
                if direct_exe.exists():
                    return direct_exe

    # 3. Unix standard locations
    unix_candidates = [
        Path.home() / ".local" / "bin" / "codex",
        Path("/usr/local/bin/codex"),
        Path("/usr/bin/codex"),
        Path.home() / ".cargo" / "bin" / "codex",
    ]
    for c in unix_candidates:
        if c.exists() and os.access(c, os.X_OK):
            return c

    return None
