"""Validated settings and provider-reported model metadata."""

from __future__ import annotations

import re
import unicodedata
from typing import Literal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    field_validator,
    model_validator,
)


class ModelInfo(BaseModel):
    """Information regarding a supported or discovered language model."""

    id: str = Field(description="Unique model slug/identifier, e.g. 'gpt-6.1-sol'")
    name: str = Field(description="Human-friendly display name, e.g. 'GPT-6.1 Sol'")
    context_window: int | None = Field(default=None, gt=0)
    max_output_tokens: int | None = Field(default=None, gt=0)
    supported_reasoning_levels: list[str] = Field(
        default_factory=list,
        description="Supported reasoning tiers",
    )
    is_custom: bool = Field(default=False, description="Whether added manually by the user")
    default_reasoning_effort: str | None = None
    reasoning_supported: bool | None = None
    thinking_supported: StrictBool | None = None
    effort_supported: StrictBool | None = None
    thinking_types: dict[
        Literal["adaptive", "enabled", "disabled", "between_tools"], StrictBool
    ] = Field(default_factory=dict)
    # Optional reviewed dialect constraints, independent of aggregate API support.
    forced_tools_supported: StrictBool | None = None
    sampling_default_only: StrictBool | None = None
    disabled_high_effort_supported: StrictBool | None = None
    tools_supported: bool | None = None
    input_modalities: list[str] = Field(default_factory=list)
    wire_api: Literal["responses", "chat", "anthropic_proxy"] | None = None

    @field_validator("id", "name")
    @classmethod
    def printable(cls, value):
        if not value.strip() or any(unicodedata.category(c) == "Cc" for c in value):
            raise ValueError("Model identifiers and labels must contain printable text.")
        return value.strip()

    @field_validator("default_reasoning_effort")
    @classmethod
    def printable_effort(cls, value):
        return cls.printable(value) if value is not None else None

    @field_validator("supported_reasoning_levels", "input_modalities")
    @classmethod
    def printable_values(cls, values):
        return list(dict.fromkeys(cls.printable(value) for value in values))


class ProviderConfig(BaseModel):
    """Configuration for an AI model provider endpoint."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Unique provider identifier, e.g. 'agentrouter'")
    codex_provider_id: str | None = None
    name: str = Field(description="Human-friendly label, e.g. 'AgentRouter'")
    base_url: str = Field(description="Normalized base URL, e.g. 'https://agentrouter.org/v1'")
    wire_api: Literal["responses", "chat", "anthropic_proxy"] = Field(
        default="responses", description="Wire protocol used by Codex"
    )
    auth_type: Literal["bearer_token", "env_var", "official_oauth", "none"] = Field(
        default="bearer_token", description="Authentication mechanism"
    )
    auth_token: str | None = Field(
        default=None,
        repr=False,
        description="API key or token string (can be an $ENV_VAR reference)",
    )
    http_headers: dict[str, str] | None = Field(default=None, repr=False)
    env_http_headers: dict[str, str] | None = Field(default=None, repr=False)
    default_model: str = Field(default="gpt-6.1-sol", description="Default model slug")
    reasoning_effort: str | None = Field(default=None, description="Explicit reasoning effort")
    proxy_port: int | None = Field(
        default=4099, description="Local loopback port for Anthropic proxy if applicable"
    )
    is_preset: bool = Field(default=False, description="Whether built-in default preset")
    models: list[ModelInfo] = Field(default_factory=list, description="Associated models list")
    model_selected: bool = True
    catalog_source: str | None = None
    catalog_fetched_at: str | None = None
    catalog_scope: str | None = Field(default=None, repr=False)

    # Advanced granular model controls
    context_window_limit: int | None = Field(
        default=None,
        gt=0,
        description="Custom context window token cap (e.g. 128000, 256000, None for model default)",
    )
    max_output_tokens: int | None = Field(
        default=None,
        gt=0,
        description="Maximum completion output tokens (e.g. 4096, 16384, None for default)",
    )
    streaming: bool = Field(
        default=True, description="Enable live response streaming (stream: true / false)"
    )
    reasoning_enabled: bool = Field(
        default=True,
        description="Enable model reasoning / chain-of-thought (reasoning: true / false)",
    )
    reasoning_budget: int | None = Field(
        default=None,
        gt=0,
        description="Explicit thinking budget in tokens (e.g. 8192, 16384 for Claude/thinking models)",
    )
    messages_summary: Literal["none", "auto"] = Field(
        default="none",
        description="Opt-in display of provider-supplied Messages thinking summaries",
    )
    messages_thinking: Literal["adaptive", "enabled", "disabled", "between_tools"] | None = Field(
        default=None,
        description="Explicit Messages thinking mode; None retains legacy budget controls",
    )

    @field_validator("id", "codex_provider_id")
    @classmethod
    def valid_id(cls, value):
        if value is not None and not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            raise ValueError("Use letters, digits, underscores or hyphens for the connection ID.")
        return value

    @field_validator("name", "default_model", "reasoning_effort")
    @classmethod
    def nonempty(cls, value):
        if value is None:
            return value
        if not value.strip() or any(ord(c) < 32 for c in value):
            raise ValueError("Value must contain printable text.")
        return value.strip()

    @field_validator("base_url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlparse(value)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.fragment:
            raise ValueError("Use a complete HTTP or HTTPS endpoint without a fragment.")
        if parsed.username or parsed.password or parsed.query:
            raise ValueError("Store credentials separately from the endpoint URL.")
        return value.rstrip("/")

    @field_validator("proxy_port")
    @classmethod
    def valid_port(cls, value):
        if value is not None and not 1024 <= value <= 65535:
            raise ValueError("Adapter port must be between 1024 and 65535.")
        return value

    @field_validator("http_headers", "env_http_headers")
    @classmethod
    def valid_headers(cls, value, info):
        for header, content in (value or {}).items():
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", header) or any(
                ord(c) < 32 for c in content
            ):
                raise ValueError("Headers must use valid names and printable values.")
            if info.field_name == "env_http_headers" and not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", content
            ):
                raise ValueError("Environment headers require an environment-variable name.")
        return value


class AppPreferences(BaseModel):
    """User preferences for tool behavior."""

    model_config = ConfigDict(extra="forbid")

    auto_backup: bool = Field(default=True, description="Create automatic session snapshots")
    confirm_bridge: bool = Field(default=True, description="Prompt before auto-bridging sessions")
    ascii_mode: bool = Field(default=False, description="Force plain ASCII symbols")
    proxy_port: int = Field(
        default=4099, ge=1024, le=65535, description="Port for local adapter proxy"
    )


class CmgrSettings(BaseModel):
    """Root schema for ~/.codex/cmgr.json."""

    model_config = ConfigDict(extra="forbid")

    version: Literal[1] = Field(default=1, description="Schema version")
    _source_bytes: bytes | None = PrivateAttr(default=None)
    _loaded: bool = PrivateAttr(default=False)
    active_provider_id: str = Field(default="openai", description="Currently active provider ID")
    providers: dict[str, ProviderConfig] = Field(
        default_factory=dict, description="Dictionary of configured providers keyed by ID"
    )
    preferences: AppPreferences = Field(
        default_factory=AppPreferences, description="Application preferences"
    )

    @model_validator(mode="after")
    def matching_provider_ids(self):
        if any(key != provider.id for key, provider in self.providers.items()):
            raise ValueError("Provider dictionary keys must match their connection IDs.")
        return self


class ExportBundle(BaseModel):
    """Portable export package schema for team/migration sharing."""

    model_config = ConfigDict(extra="forbid")
    version: Literal[1] = 1
    exported_at: str
    active_provider_id: str
    providers: dict[str, ProviderConfig]
    is_sanitized: bool = Field(
        default=True, description="True if credential fields were removed during export"
    )

    @model_validator(mode="after")
    def valid_identity(self):
        if self.active_provider_id not in self.providers or any(
            pid != provider.id for pid, provider in self.providers.items()
        ):
            raise ValueError("Bundle identities must match their provider definitions.")
        return self
