"""Non-mutating settings reads and guarded persistence with revision snapshots."""

from __future__ import annotations

import json

from pydantic import ValidationError

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.core.profiles import connection_id, effective_configuration, read_config_toml
from codex_mgr.core.providers import get_default_presets
from codex_mgr.utils.files import (
    FileConflictError,
    atomic_write,
    create_snapshot,
    mutation_lock,
    read_optional,
)
from codex_mgr.utils.paths import get_backups_dir, get_cmgr_settings_path, get_config_toml_path


def _read_valid(raw: bytes) -> CmgrSettings:
    try:
        return CmgrSettings.model_validate_json(raw)
    except (ValidationError, ValueError) as exc:
        raise ValueError(
            "Invalid manager settings; repair cmgr.json or restore a revision before retrying."
        ) from exc


def serialize_settings(settings: CmgrSettings) -> bytes:
    valid = CmgrSettings.model_validate(settings.model_dump())
    return (json.dumps(valid.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n").encode(
        "utf-8"
    )


def _import_configuration(settings: CmgrSettings) -> None:
    if not get_config_toml_path().exists():
        return
    raw = read_config_toml()
    effective = effective_configuration(raw)
    selected = effective.get("model_provider", "openai")
    matches = [p for p in settings.providers.values() if connection_id(p) == selected]
    active = settings.providers.get(settings.active_provider_id)
    if active in matches:
        existing = active
    elif len(matches) > 1:
        raise ValueError(
            "Several manager connections share the selected Codex ID. Resolve their identities before applying changes."
        )
    else:
        existing = matches[0] if matches else None
    table = raw.get("model_providers", {}).get(selected, {})
    if existing is None:
        if selected == "openai" and not table:
            identifier = "openai_oauth" if "openai" in settings.providers else "openai"
            existing = ProviderConfig(
                id=identifier,
                name="ChatGPT sign-in",
                base_url="https://api.openai.com/v1",
                auth_type="official_oauth",
                default_model=effective.get("model", "coding-model"),
                model_selected=bool(effective.get("model")),
            )
            settings.providers[identifier] = existing
        else:
            existing = _provider_from_table(selected, table, effective)
            settings.providers[existing.id] = existing
    settings.active_provider_id = existing.id
    if effective.get("model"):
        existing.default_model = effective["model"]
        existing.model_selected = True
    if effective.get("model_reasoning_effort"):
        existing.reasoning_effort = effective["model_reasoning_effort"]
    existing.context_window_limit = effective.get("model_context_window")
    if table.get("base_url") and existing.auth_type != "official_oauth":
        existing.base_url = table["base_url"]
    if existing.auth_type != "official_oauth":
        existing.wire_api = table.get("wire_api", "responses")
        existing.http_headers = table.get("http_headers")
        existing.env_http_headers = table.get("env_http_headers")
        if table.get("env_key"):
            existing.auth_token = "$" + table["env_key"]
            existing.auth_type = "env_var"
        elif table.get("experimental_bearer_token"):
            existing.auth_token = table["experimental_bearer_token"]
            existing.auth_type = "bearer_token"
        else:
            existing.auth_token = None
            existing.auth_type = (
                "bearer_token" if existing.http_headers or existing.env_http_headers else "none"
            )
    settings.providers[existing.id] = ProviderConfig.model_validate(existing.model_dump())


def _provider_from_table(selected, table, effective):
    base = table.get("base_url")
    if not base:
        if selected == "ollama":
            base = "http://localhost:11434/v1"
        elif selected == "lmstudio":
            base = "http://localhost:1234/v1"
        else:
            raise ValueError("The selected connection has no endpoint definition.")
    env = table.get("env_key")
    token = f"${env}" if env else table.get("experimental_bearer_token")
    existing = ProviderConfig(
        id=selected,
        codex_provider_id=selected,
        name=table.get("name", selected),
        base_url=base,
        auth_type="env_var" if env else "bearer_token" if token else "none",
        auth_token=token,
        wire_api=table.get("wire_api", "responses"),
        default_model=effective.get("model", "coding-model"),
        model_selected=bool(effective.get("model")),
        http_headers=table.get("http_headers"),
        env_http_headers=table.get("env_http_headers"),
    )
    return existing


def load_settings() -> CmgrSettings:
    path = get_cmgr_settings_path()
    raw = read_optional(path)
    settings = (
        _read_valid(raw)
        if raw is not None
        else CmgrSettings(providers={"openai": get_default_presets()["openai"]})
    )
    try:
        _import_configuration(settings)
    except (ValidationError, TypeError, AttributeError, KeyError) as exc:
        raise ValueError(
            "Invalid selected Codex connection; repair its definition before changing settings."
        ) from exc
    settings._source_bytes = raw
    settings._loaded = True
    return settings


def commit_settings(
    settings: CmgrSettings, *, expected: bytes | None, create_backup: bool = True
) -> bytes:
    """Persist while the caller holds the settings lock."""
    path = get_cmgr_settings_path()
    current = read_optional(path)
    if current != expected:
        raise FileConflictError("Settings changed; reload before saving.")
    if current is not None:
        _read_valid(current)
    encoded = serialize_settings(settings)
    if current == encoded:
        settings._source_bytes = current
        settings._loaded = True
        return encoded
    if current is not None and create_backup:
        create_snapshot(path, get_backups_dir() / "settings", prefix="cmgr_", suffix=".json")
        atomic_write(path.with_suffix(".json.bak"), current)
    atomic_write(path, encoded, expected=current)
    settings._source_bytes = encoded
    settings._loaded = True
    return encoded


def save_settings(settings: CmgrSettings, create_backup: bool = True) -> None:
    path = get_cmgr_settings_path()
    with mutation_lock(path):
        expected = settings._source_bytes if settings._loaded else read_optional(path)
        commit_settings(settings, expected=expected, create_backup=create_backup)
