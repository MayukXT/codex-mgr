"""Surgical config.toml orchestrator.

Updates the active model provider and model settings while strictly preserving
all custom MCP servers (e.g. kaggle, firecrawl), hooks, desktop themes, and trusted paths.
"""

from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.utils.paths import get_backups_dir, get_config_toml_path
from codex_mgr.utils.toml_writer import dump_toml


def read_config_toml() -> Dict[str, Any]:
    """Parse ~/.codex/config.toml into a Python dictionary."""
    config_path = get_config_toml_path()
    if not config_path.exists():
        return {}
    try:
        content = config_path.read_bytes()
        return tomllib.loads(content.decode("utf-8"))
    except Exception:
        return {}


def switch_active_provider(provider: ProviderConfig) -> Tuple[bool, str]:
    """Surgically update config.toml to point to the specified provider.

    Preserves 100% of user MCP servers, desktop appearance, hooks, and project trust.
    """
    config_path = get_config_toml_path()
    if not config_path.exists():
        return False, f"config.toml does not exist at {config_path}"

    # 1. Create safety backup
    try:
        bak_file = config_path.with_suffix(".toml.bak")
        shutil.copy2(config_path, bak_file)

        # Snapshot in ~/.codex/backups/config/
        snap_dir = get_backups_dir() / "config"
        snap_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shutil.copy2(config_path, snap_dir / f"config_{ts}.toml")
    except Exception as e:
        return False, f"Could not create pre-switch backup: {str(e)}"

    # 2. Read existing configuration
    data = read_config_toml()

    # 3. Apply updates to model and reasoning effort
    data["model"] = provider.default_model
    data["model_reasoning_effort"] = provider.reasoning_effort

    # 4. Configure provider blocks
    if provider.id == "openai":
        # Official OpenAI does not specify model_provider at top level
        data.pop("model_provider", None)
    else:
        data["model_provider"] = provider.id

        if "model_providers" not in data or not isinstance(data["model_providers"], dict):
            data["model_providers"] = {}

        # Configure provider endpoint details
        p_table: Dict[str, Any] = {
            "name": provider.name,
            "wire_api": "responses" if provider.wire_api == "responses" else "chat",
        }

        # If Anthropic proxy, point base_url to local loopback
        if provider.wire_api == "anthropic_proxy":
            port = provider.proxy_port or 4099
            p_table["base_url"] = f"http://127.0.0.1:{port}/v1"
            p_table["wire_api"] = "responses"
        else:
            p_table["base_url"] = provider.base_url

        # Authentication credentials
        if provider.auth_token:
            if provider.auth_token.startswith("$"):
                p_table["bearer_token_env_var"] = provider.auth_token[1:]
            else:
                p_table["experimental_bearer_token"] = provider.auth_token

        data["model_providers"][provider.id] = p_table

    # 5. Atomic write
    tmp_path = config_path.with_suffix(".toml.tmp")
    try:
        toml_string = dump_toml(data)
        tmp_path.write_text(toml_string, encoding="utf-8")
        tmp_path.replace(config_path)
        return True, f"Switched to {provider.name} (Model: {provider.default_model})"
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        return False, f"Failed to write config.toml: {str(exc)}"
