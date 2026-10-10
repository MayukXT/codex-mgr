"""Sanitized-by-default exports and validated, collision-safe imports."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from codex_mgr.config.schema import ExportBundle
from codex_mgr.config.settings import load_settings
from codex_mgr.core.activation import apply_settings
from codex_mgr.core.profiles import connection_id
from codex_mgr.utils.files import atomic_write, mutation_lock, read_optional
from codex_mgr.utils.paths import get_codex_home


def export_bundle(output_path: Path, sanitize_keys=True, *, overwrite=False):
    settings = load_settings()
    target = output_path.resolve()
    home = get_codex_home()
    protected = {
        "auth.json",
        "config.toml",
        "cmgr.json",
        "models_cache.json",
        "session_index.jsonl",
        "cmgr-adapter-continuations.key",
        "cmgr-adapter-continuations.key.sha256",
        "cmgr-adapter-continuations.key.cmgr.lock",
    }
    if (
        target.parent == home
        and target.name in protected
        or any(
            folder in target.parents
            for folder in (home / "sessions", home / "archived_sessions", home / "backups")
        )
    ):
        raise ValueError("Choose an export path outside Codex runtime files and backups.")
    providers = {}
    for pid, provider in settings.providers.items():
        item = provider.model_copy(deep=True)
        if sanitize_keys:
            if item.auth_token and not item.auth_token.startswith("$"):
                item.auth_token = None
            item.http_headers = None
            item.catalog_scope = None
            item.catalog_source = None
            item.catalog_fetched_at = None
        providers[pid] = item
    bundle = ExportBundle(
        exported_at=datetime.now(timezone.utc).isoformat(),
        active_provider_id=settings.active_provider_id,
        providers=providers,
        is_sanitized=sanitize_keys,
    )
    encoded = (
        json.dumps(bundle.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n"
    ).encode()
    with mutation_lock(target):
        before = read_optional(target)
        if before is not None and not overwrite:
            raise ValueError("Export file exists. Choose another filename or request overwrite.")
        atomic_write(target, encoded, expected=before)
    return output_path


def read_import_bundle(path):
    try:
        return ExportBundle.model_validate_json(path.read_bytes())
    except (ValidationError, ValueError) as exc:
        raise ValueError(
            "Invalid import bundle; check schema version, identities and connection fields."
        ) from exc


def inspect_import_bundle(bundle_path):
    bundle = read_import_bundle(bundle_path)
    settings = load_settings()
    return (
        [pid for pid in bundle.providers if pid not in settings.providers],
        [pid for pid in bundle.providers if pid in settings.providers],
    )


def _new_identity(settings, original):
    stem = original[:110] + "_imported"
    identifier = stem
    suffix = 2
    used = {connection_id(p) for p in settings.providers.values()}
    while identifier in settings.providers or identifier in used:
        identifier = f"{stem}_{suffix}"
        suffix += 1
    return identifier


def apply_import_bundle(bundle_path, conflict_strategy="merge"):
    if conflict_strategy not in {"merge", "overwrite", "rename"}:
        raise ValueError("Choose merge, overwrite or rename.")
    bundle = read_import_bundle(bundle_path)
    settings = load_settings()
    added = updated = 0
    active_changed = False
    for pid, incoming in bundle.providers.items():
        provider = incoming.model_copy(deep=True)
        current = settings.providers.get(pid)
        if current is not None and conflict_strategy == "merge":
            existing = {m.id for m in current.models}
            changed = False
            for model in provider.models:
                if model.id not in existing:
                    current.models.append(model)
                    existing.add(model.id)
                    changed = True
            updated += int(changed)
            continue
        if current is not None and conflict_strategy == "rename":
            provider.id = _new_identity(settings, pid)
            provider.codex_provider_id = provider.id
            settings.providers[provider.id] = provider
            added += 1
            continue
        if current is None:
            used = {connection_id(p) for p in settings.providers.values()}
            if provider.auth_type != "official_oauth" and connection_id(provider) in used:
                provider.codex_provider_id = provider.id
                if connection_id(provider) in used:
                    raise ValueError(
                        "Imported connection conflicts with an existing Codex connection ID. Rename it first."
                    )
            settings.providers[pid] = provider
            added += 1
        else:
            if bundle.is_sanitized:
                if (
                    provider.auth_token is None
                    and provider.auth_type in {"bearer_token", "env_var"}
                    and current.auth_type in {"bearer_token", "env_var"}
                ):
                    provider.auth_token = current.auth_token
                    provider.auth_type = current.auth_type
                if provider.http_headers is None:
                    provider.http_headers = current.http_headers
            # Keep the connection identity already associated with the saved entry.
            provider.codex_provider_id = current.codex_provider_id
            settings.providers[pid] = provider
            updated += 1
            active_changed |= pid == settings.active_provider_id
    ok, message = apply_settings(
        settings, activate_provider_id=settings.active_provider_id if active_changed else None
    )
    if not ok:
        raise ValueError(message)
    return added, updated
