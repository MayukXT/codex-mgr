"""Connection templates and guarded provider management."""

from __future__ import annotations

from codex_mgr.config.schema import ProviderConfig


def get_default_presets() -> dict[str, ProviderConfig]:
    """Templates provide endpoints, never assumed model entitlements or limits."""
    definitions = [
        (
            "openai",
            "ChatGPT sign-in",
            "https://api.openai.com/v1",
            "responses",
            "official_oauth",
            None,
        ),
        (
            "openai_api",
            "OpenAI API key",
            "https://api.openai.com/v1",
            "responses",
            "env_var",
            "$OPENAI_API_KEY",
        ),
        (
            "anthropic",
            "Anthropic API key",
            "https://api.anthropic.com",
            "anthropic_proxy",
            "env_var",
            "$ANTHROPIC_API_KEY",
        ),
        (
            "openrouter",
            "OpenRouter",
            "https://openrouter.ai/api/v1",
            "responses",
            "env_var",
            "$OPENROUTER_API_KEY",
        ),
        (
            "opencode",
            "OpenCode Zen",
            "https://opencode.ai/zen/v1",
            "responses",
            "env_var",
            "$OPENCODE_API_KEY",
        ),
        (
            "agentrouter",
            "AgentRouter",
            "https://agentrouter.org/v1",
            "responses",
            "env_var",
            "$AGENTROUTER_API_KEY",
        ),
        (
            "nvidia",
            "NVIDIA NIM",
            "https://integrate.api.nvidia.com/v1",
            "chat",
            "env_var",
            "$NVIDIA_API_KEY",
        ),
        (
            "deepseek",
            "DeepSeek",
            "https://api.deepseek.com/v1",
            "chat",
            "env_var",
            "$DEEPSEEK_API_KEY",
        ),
        ("groq", "Groq", "https://api.groq.com/openai/v1", "chat", "env_var", "$GROQ_API_KEY"),
        (
            "gemini",
            "Google Gemini",
            "https://generativelanguage.googleapis.com/v1beta/openai",
            "chat",
            "env_var",
            "$GEMINI_API_KEY",
        ),
        ("ollama", "Ollama", "http://localhost:11434/v1", "chat", "none", None),
        ("lmstudio", "LM Studio", "http://localhost:1234/v1", "chat", "none", None),
    ]
    return {
        identifier: ProviderConfig(
            id=identifier,
            name=name,
            base_url=url,
            wire_api=protocol,
            auth_type=auth,
            auth_token=token,
            default_model="choose-model",
            model_selected=False,
            is_preset=True,
        )
        for identifier, name, url, protocol, auth, token in definitions
    }


def store_provider(settings, provider, *, replace=False, activate=False):
    from codex_mgr.core.activation import apply_settings

    provider = ProviderConfig.model_validate(provider.model_dump())
    current = settings.providers.get(provider.id)
    if current is not None and current.model_selected and not replace:
        return False, "That connection ID already exists. Edit it or choose another ID."
    settings.providers[provider.id] = provider
    ok, message = apply_settings(
        settings,
        activate_provider_id=provider.id
        if activate or settings.active_provider_id == provider.id
        else None,
    )
    if not ok:
        if current is None:
            settings.providers.pop(provider.id, None)
        else:
            settings.providers[provider.id] = current
    return ok, message


def delete_provider(settings, provider_id):
    from codex_mgr.core.activation import apply_settings

    if provider_id == settings.active_provider_id:
        return False, "Select another connection before deleting the active one."
    current = settings.providers.pop(provider_id, None)
    if current is None:
        return False, "Connection not found."
    ok, message = apply_settings(settings)
    if not ok:
        settings.providers[provider_id] = current
    return ok, message
