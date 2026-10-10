"""Generate a custom model catalog for Codex 0.159's model metadata contract."""

import json
from pathlib import Path

from codex_mgr.core.models import model_metadata
from codex_mgr.utils.files import atomic_write


def catalog_document(provider, *, instructions):
    if not isinstance(instructions, str) or not instructions.strip():
        raise ValueError("Supply an explicit instruction template; Codex catalogs require it.")
    models = []
    for entry in provider.models:
        model = model_metadata(provider, entry.id)
        models.append(
            {
                "slug": model.id,
                "display_name": model.name,
                "description": None,
                "model_messages": {"instructions_template": instructions},
                "default_reasoning_level": model.default_reasoning_effort,
                "supported_reasoning_levels": [
                    {"effort": effort, "description": effort}
                    for effort in model.supported_reasoning_levels
                ],
                "shell_type": "unified_exec",
                "visibility": "list",
                "supported_in_api": True,
                "priority": 0,
                "availability_nux": None,
                "upgrade": None,
                "support_verbosity": False,
                "default_verbosity": None,
                "apply_patch_tool_type": None,
                "truncation_policy": {"mode": "bytes", "limit": 10000},
                "experimental_supported_tools": [],
                "context_window": model.context_window,
                "input_modalities": model.input_modalities or ["text"],
                "supports_reasoning_summary_parameter": False,
                "include_apps_usage_instructions": False,
            }
        )
    if not models:
        raise ValueError("Add or discover models before creating a catalog.")
    return {"models": models}


def export_codex_catalog(provider, path: Path, *, instructions):
    encoded = (
        json.dumps(
            catalog_document(provider, instructions=instructions), indent=2, ensure_ascii=False
        )
        + "\n"
    ).encode()
    if path.exists():
        raise ValueError("Choose a new catalog filename; existing files are preserved.")
    atomic_write(path, encoded, expected=None)
    return path
