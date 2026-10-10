"""Validated, comment-preserving Codex connection configuration."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

import tomlkit
from tomlkit.exceptions import TOMLKitError

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.utils.files import atomic_write, create_snapshot, read_optional
from codex_mgr.utils.paths import get_backups_dir, get_config_toml_path


class ConfigurationError(ValueError):
    pass


def parse_configuration(raw: bytes | None):
    try:
        doc = tomlkit.parse((raw or b"").decode("utf-8"))
        for key in ("model_providers", "profiles"):
            if key in doc and (
                not isinstance(doc[key], Mapping)
                or any(not isinstance(v, Mapping) for v in doc[key].values())
            ):
                raise ConfigurationError(f"The {key} setting must contain named tables.")
        return doc
    except (UnicodeDecodeError, TOMLKitError) as exc:
        raise ConfigurationError(
            "Invalid config.toml; repair it before changing providers."
        ) from exc


def read_config_toml() -> dict[str, Any]:
    return parse_configuration(read_optional(get_config_toml_path())).unwrap()


def effective_configuration(data: dict[str, Any]) -> dict[str, Any]:
    effective = dict(data)
    profile = data.get("profile")
    if profile:
        selected = data.get("profiles", {}).get(profile)
        if not isinstance(selected, dict):
            raise ConfigurationError("The selected Codex profile does not exist.")
        effective.update(selected)
    return effective


def connection_id(provider: ProviderConfig) -> str:
    if provider.auth_type == "official_oauth":
        return "openai"
    chosen = provider.codex_provider_id or provider.id
    return f"cmgr_{chosen}" if chosen in {"openai", "ollama", "lmstudio"} else chosen


def prepare_configuration(provider: ProviderConfig, raw: bytes | None) -> bytes:
    doc = parse_configuration(raw)
    # Revalidate mutable settings before producing any side effects.
    provider = ProviderConfig.model_validate(provider.model_dump())
    if not provider.model_selected:
        raise ConfigurationError("Choose an exact model ID before activating this template.")
    if (
        provider.auth_type in {"bearer_token", "env_var"}
        and not provider.auth_token
        and not provider.http_headers
        and not provider.env_http_headers
    ):
        raise ConfigurationError(
            "Supply the selected API credential, or explicitly choose no authentication."
        )
    if provider.wire_api != "responses":
        raise ConfigurationError(
            "This connection needs a verified Responses adapter before activation."
        )
    if (
        not provider.streaming
        or provider.max_output_tokens is not None
        or provider.reasoning_budget is not None
    ):
        raise ConfigurationError(
            "Streaming, output caps and thinking budgets are adapter controls for this connection."
        )
    force_login = doc.get("forced_login_method")
    if force_login == "chatgpt" and provider.auth_type != "official_oauth":
        raise ConfigurationError("Codex is configured to require ChatGPT sign-in.")
    if force_login == "api" and provider.auth_type == "official_oauth":
        raise ConfigurationError("Codex is configured to require API-key authentication.")
    target = doc
    if doc.get("profile"):
        profile = str(doc["profile"])
        if profile not in doc.get("profiles", {}):
            raise ConfigurationError("The selected Codex profile does not exist.")
        target = doc["profiles"][profile]
    target["model"] = provider.default_model
    effort = provider.reasoning_effort if provider.reasoning_enabled else "none"
    model = next((m for m in provider.models if m.id == provider.default_model), None)
    if (
        model
        and model.supported_reasoning_levels
        and effort not in model.supported_reasoning_levels
    ):
        raise ConfigurationError("The selected model does not advertise this reasoning effort.")
    if effort is None:
        if target is not doc and doc.get("model_reasoning_effort") is not None:
            raise ConfigurationError(
                "This profile inherits global reasoning. Choose an explicit effort or remove the global setting."
            )
        target.pop("model_reasoning_effort", None)
    else:
        target["model_reasoning_effort"] = effort
    for key in ("model_reasoning", "stream", "max_tokens"):
        target.pop(key, None)
    if provider.context_window_limit is not None:
        target["model_context_window"] = provider.context_window_limit
    else:
        if target is not doc and doc.get("model_context_window") is not None:
            raise ConfigurationError(
                "This profile inherits a global context limit. Change the global limit explicitly or set a profile limit."
            )
        target.pop("model_context_window", None)
    chosen_id = connection_id(provider)
    target["model_provider"] = chosen_id
    if provider.auth_type == "official_oauth":
        override = doc.get("model_providers", {}).get("openai", {})
        if (
            any(k in override for k in ("base_url", "env_key", "experimental_bearer_token"))
            or override.get("requires_openai_auth") is False
        ):
            raise ConfigurationError(
                "An existing OpenAI connection override conflicts with ChatGPT sign-in. Preserve it as a separate custom connection first."
            )
    else:
        if "model_providers" not in doc:
            doc["model_providers"] = tomlkit.table()
        if chosen_id not in doc["model_providers"]:
            doc["model_providers"][chosen_id] = tomlkit.table()
        table = doc["model_providers"][chosen_id]
        table["name"] = provider.name
        table["base_url"] = provider.base_url
        table["wire_api"] = "responses"
        for key in (
            "stream",
            "context_window",
            "max_tokens",
            "reasoning_budget",
            "bearer_token_env_var",
            "env_key",
            "experimental_bearer_token",
        ):
            table.pop(key, None)
        table["requires_openai_auth"] = False
        if provider.auth_type != "none":
            token = provider.auth_token
            if not token and not provider.http_headers and not provider.env_http_headers:
                raise ConfigurationError(
                    "An API key or environment-variable reference is required."
                )
            if token and token.startswith("$"):
                variable = token[1:]
                if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", variable):
                    raise ConfigurationError("Invalid credential environment-variable name.")
                table["env_key"] = variable
            elif token and provider.auth_type == "env_var":
                raise ConfigurationError("An environment credential must use $VARIABLE syntax.")
            elif token:
                table["experimental_bearer_token"] = token
        if provider.http_headers is not None:
            table["http_headers"] = provider.http_headers
        if provider.env_http_headers is not None:
            table["env_http_headers"] = provider.env_http_headers
    encoded = tomlkit.dumps(doc).encode("utf-8")
    parse_configuration(encoded)
    return encoded


def snapshot_configuration() -> None:
    path = get_config_toml_path()
    if path.exists():
        create_snapshot(path, get_backups_dir() / "config", prefix="config_", suffix=".toml")
        atomic_write(path.with_suffix(".toml.bak"), path.read_bytes())


def switch_active_provider(provider: ProviderConfig) -> tuple[bool, str]:
    """Compatibility entry point using the same verified activation transaction."""
    from codex_mgr.config.settings import load_settings
    from codex_mgr.core.activation import apply_settings

    try:
        settings = load_settings()
        settings.providers[provider.id] = provider
        return apply_settings(settings, activate_provider_id=provider.id)
    except ConfigurationError as exc:
        return False, str(exc)
    except (OSError, ValueError, RuntimeError):
        return (
            False,
            "Configuration was not applied. Check syntax, credentials, supported controls, permissions, or another writer.",
        )
