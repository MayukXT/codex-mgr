"""Data models and schemas for codex-mgr settings, providers, and models.

Designed for 2026 model generation with full context window tracking,
multi-tier reasoning levels (up to 'ultra'), and portable configuration.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class ModelInfo(BaseModel):
    """Information regarding a supported or discovered language model."""

    id: str = Field(description="Unique model slug/identifier, e.g. 'gpt-6.1-sol'")
    name: str = Field(description="Human-friendly display name, e.g. 'GPT-6.1 Sol'")
    context_window: int = Field(default=256000, description="Context window capacity in tokens")
    supported_reasoning_levels: List[str] = Field(
        default_factory=lambda: ["low", "medium", "high", "xhigh", "max"],
        description="Supported reasoning tiers"
    )
    is_custom: bool = Field(default=False, description="Whether added manually by the user")


class ProviderConfig(BaseModel):
    """Configuration for an AI model provider endpoint."""

    id: str = Field(description="Unique provider identifier, e.g. 'agentrouter'")
    name: str = Field(description="Human-friendly label, e.g. 'AgentRouter'")
    base_url: str = Field(description="Normalized base URL, e.g. 'https://agentrouter.org/v1'")
    wire_api: Literal["responses", "chat", "anthropic_proxy"] = Field(
        default="responses",
        description="Wire protocol used by Codex"
    )
    auth_type: Literal["bearer_token", "env_var", "official_oauth", "none"] = Field(
        default="bearer_token",
        description="Authentication mechanism"
    )
    auth_token: Optional[str] = Field(
        default=None,
        description="API key or token string (can be an $ENV_VAR reference)"
    )
    default_model: str = Field(default="gpt-6.1-sol", description="Default model slug")
    reasoning_effort: str = Field(default="medium", description="Default reasoning effort")
    proxy_port: Optional[int] = Field(
        default=4099,
        description="Local loopback port for Anthropic proxy if applicable"
    )
    is_preset: bool = Field(default=False, description="Whether built-in default preset")
    models: List[ModelInfo] = Field(default_factory=list, description="Associated models list")


class AppPreferences(BaseModel):
    """User preferences for tool behavior."""

    auto_backup: bool = Field(default=True, description="Create automatic session snapshots")
    confirm_bridge: bool = Field(default=True, description="Prompt before auto-bridging sessions")
    ascii_mode: bool = Field(default=False, description="Force plain ASCII symbols")
    proxy_port: int = Field(default=4099, description="Port for local adapter proxy")


class CmgrSettings(BaseModel):
    """Root schema for ~/.codex/cmgr.json."""

    version: int = Field(default=1, description="Schema version")
    active_provider_id: str = Field(default="openai", description="Currently active provider ID")
    providers: Dict[str, ProviderConfig] = Field(
        default_factory=dict,
        description="Dictionary of configured providers keyed by ID"
    )
    preferences: AppPreferences = Field(
        default_factory=AppPreferences,
        description="Application preferences"
    )


class ExportBundle(BaseModel):
    """Portable export package schema for team/migration sharing."""

    version: int = 1
    exported_at: str
    active_provider_id: str
    providers: Dict[str, ProviderConfig]
    is_sanitized: bool = Field(
        default=False,
        description="True if sensitive API keys were stripped during export"
    )
