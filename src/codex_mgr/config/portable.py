"""Export and Import engine for codex-mgr configuration bundles.

Provides safe portability across machines with optional key sanitization
and interactive conflict diffing.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Literal, Tuple

from codex_mgr.config.schema import ExportBundle, ProviderConfig
from codex_mgr.config.settings import load_settings, save_settings


def export_bundle(output_path: Path, sanitize_keys: bool = False) -> Path:
    """Export current providers and configurations to a portable JSON bundle."""
    settings = load_settings()

    exported_providers: Dict[str, ProviderConfig] = {}
    for pid, p in settings.providers.items():
        p_copy = p.model_copy(deep=True)
        if sanitize_keys and p_copy.auth_token and not p_copy.auth_token.startswith("$"):
            p_copy.auth_token = None
        exported_providers[pid] = p_copy

    bundle = ExportBundle(
        version=1,
        exported_at=datetime.now().isoformat(),
        active_provider_id=settings.active_provider_id,
        providers=exported_providers,
        is_sanitized=sanitize_keys,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    formatted = json.dumps(bundle.model_dump(mode="json"), indent=2, ensure_ascii=False)
    output_path.write_text(formatted, encoding="utf-8")
    return output_path


def inspect_import_bundle(bundle_path: Path) -> Tuple[List[str], List[str]]:
    """Inspect an export bundle before importing.

    Returns:
        (new_provider_names, conflicting_provider_names)
    """
    content = bundle_path.read_text(encoding="utf-8")
    raw = json.loads(content)
    bundle = ExportBundle.model_validate(raw)

    settings = load_settings()
    new_providers: List[str] = []
    conflicts: List[str] = []

    for pid in bundle.providers:
        if pid in settings.providers:
            conflicts.append(pid)
        else:
            new_providers.append(pid)

    return new_providers, conflicts


def apply_import_bundle(
    bundle_path: Path,
    conflict_strategy: Literal["merge", "overwrite", "rename"] = "merge"
) -> Tuple[int, int]:
    """Import and apply providers from an export bundle.

    Returns:
        (count_added, count_updated)
    """
    content = bundle_path.read_text(encoding="utf-8")
    bundle = ExportBundle.model_validate(json.loads(content))
    settings = load_settings()

    added = 0
    updated = 0

    for pid, provider in bundle.providers.items():
        if pid not in settings.providers:
            settings.providers[pid] = provider
            added += 1
        else:
            if conflict_strategy == "overwrite":
                settings.providers[pid] = provider
                updated += 1
            elif conflict_strategy == "rename":
                new_id = f"{pid}_imported"
                provider.id = new_id
                provider.name = f"{provider.name} (Imported)"
                settings.providers[new_id] = provider
                added += 1
            elif conflict_strategy == "merge":
                # Merge models list
                existing = settings.providers[pid]
                existing_model_ids = {m.id for m in existing.models}
                for m in provider.models:
                    if m.id not in existing_model_ids:
                        existing.models.append(m)
                        updated += 1

    save_settings(settings, create_backup=True)
    return added, updated
