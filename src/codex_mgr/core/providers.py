"""Provider registry and 2026 preset templates.

Inspired by Cline, Roo Code, and Continue.dev, offering extensive pre-configured
gateways and offline endpoints out of the box with complete customization freedom.
"""

from __future__ import annotations

from typing import Dict
from codex_mgr.config.schema import ModelInfo, ProviderConfig


def get_default_presets() -> Dict[str, ProviderConfig]:
    """Return the dictionary of built-in 2026 provider presets."""
    return {
        "openai": ProviderConfig(
            id="openai",
            name="OpenAI (Official)",
            base_url="https://api.openai.com/v1",
            wire_api="responses",
            auth_type="official_oauth",
            auth_token=None,
            default_model="gpt-6.1-sol",
            reasoning_effort="low",
            is_preset=True,
            models=[
                ModelInfo(
                    id="gpt-6.1-sol",
                    name="GPT-6.1 Sol",
                    context_window=512000,
                    supported_reasoning_levels=["low", "medium", "high", "xhigh", "max", "ultra"]
                ),
                ModelInfo(
                    id="gpt-6-astra",
                    name="GPT-6 Astra",
                    context_window=512000,
                    supported_reasoning_levels=["low", "medium", "high", "xhigh", "max", "ultra"]
                ),
                ModelInfo(
                    id="gpt-6-sol",
                    name="GPT-6 Sol",
                    context_window=512000,
                    supported_reasoning_levels=["low", "medium", "high", "xhigh", "max", "ultra"]
                ),
                ModelInfo(
                    id="gpt-5.6-terra",
                    name="GPT-5.6 Terra",
                    context_window=256000,
                    supported_reasoning_levels=["low", "medium", "high", "xhigh", "max", "ultra"]
                ),
                ModelInfo(
                    id="gpt-5.6-sol",
                    name="GPT-5.6 Sol",
                    context_window=256000,
                    supported_reasoning_levels=["low", "medium", "high", "xhigh", "max", "ultra"]
                ),
            ]
        ),
        "anthropic": ProviderConfig(
            id="anthropic",
            name="Anthropic (Direct Claude 5.5)",
            base_url="https://api.anthropic.com",
            wire_api="anthropic_proxy",
            auth_type="bearer_token",
            auth_token=None,
            default_model="claude-5-5-sonnet-20260928",
            reasoning_effort="high",
            proxy_port=4099,
            is_preset=True,
            models=[
                ModelInfo(
                    id="claude-5-5-sonnet-20260928",
                    name="Claude Sonnet 5.5",
                    context_window=1000000,
                    supported_reasoning_levels=["low", "medium", "high", "max"]
                ),
                ModelInfo(
                    id="claude-5-5-opus-20260922",
                    name="Claude Opus 5.5",
                    context_window=1000000,
                    supported_reasoning_levels=["low", "medium", "high", "max"]
                ),
                ModelInfo(
                    id="claude-5-1-fable-20260901",
                    name="Claude Fable 5.1",
                    context_window=1000000,
                    supported_reasoning_levels=["low", "medium", "high", "max"]
                ),
                ModelInfo(
                    id="claude-5-5-haiku-20261007",
                    name="Claude Haiku 5.5",
                    context_window=1000000,
                    supported_reasoning_levels=["low", "medium"]
                ),
            ]
        ),
        "agentrouter": ProviderConfig(
            id="agentrouter",
            name="AgentRouter",
            base_url="https://agentrouter.org/v1",
            wire_api="responses",
            auth_type="bearer_token",
            auth_token=None,
            default_model="gpt-5.6-sol",
            reasoning_effort="max",
            is_preset=True,
            models=[
                ModelInfo(id="gpt-5.6-sol", name="GPT-5.6 Sol", context_window=256000),
                ModelInfo(id="gpt-6.1-sol", name="GPT-6.1 Sol", context_window=512000),
                ModelInfo(id="claude-5-5-sonnet", name="Claude Sonnet 5.5", context_window=1000000),
            ]
        ),
        "openrouter": ProviderConfig(
            id="openrouter",
            name="OpenRouter",
            base_url="https://openrouter.ai/api/v1",
            wire_api="responses",
            auth_type="bearer_token",
            auth_token=None,
            default_model="anthropic/claude-5-5-sonnet",
            reasoning_effort="high",
            is_preset=True,
            models=[
                ModelInfo(id="anthropic/claude-5-5-sonnet", name="Claude Sonnet 5.5", context_window=1000000),
                ModelInfo(id="openai/gpt-6.1-sol", name="GPT-6.1 Sol", context_window=512000),
                ModelInfo(id="deepseek/deepseek-r2", name="DeepSeek R2", context_window=128000),
            ]
        ),
        "deepseek": ProviderConfig(
            id="deepseek",
            name="DeepSeek (Official)",
            base_url="https://api.deepseek.com/v1",
            wire_api="chat",
            auth_type="bearer_token",
            auth_token=None,
            default_model="deepseek-r2",
            reasoning_effort="max",
            is_preset=True,
            models=[
                ModelInfo(id="deepseek-r2", name="DeepSeek R2 (Reasoning)", context_window=128000),
                ModelInfo(id="deepseek-chat", name="DeepSeek Chat V3", context_window=128000),
            ]
        ),
        "groq": ProviderConfig(
            id="groq",
            name="Groq (Ultra-Fast LPU)",
            base_url="https://api.groq.com/openai/v1",
            wire_api="chat",
            auth_type="bearer_token",
            auth_token=None,
            default_model="llama-3.3-70b-versatile",
            reasoning_effort="medium",
            is_preset=True,
            models=[
                ModelInfo(id="llama-3.3-70b-versatile", name="Llama 3.3 70B", context_window=128000),
            ]
        ),
        "gemini": ProviderConfig(
            id="gemini",
            name="Google Gemini (OpenAI Compatible)",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai",
            wire_api="chat",
            auth_type="bearer_token",
            auth_token=None,
            default_model="gemini-2.5-pro",
            reasoning_effort="high",
            is_preset=True,
            models=[
                ModelInfo(id="gemini-2.5-pro", name="Gemini 2.5 Pro", context_window=1000000),
                ModelInfo(id="gemini-2.5-flash", name="Gemini 2.5 Flash", context_window=1000000),
            ]
        ),
        "ollama": ProviderConfig(
            id="ollama",
            name="Ollama (Local Offline)",
            base_url="http://localhost:11434/v1",
            wire_api="chat",
            auth_type="none",
            auth_token=None,
            default_model="llama-3.3-70b",
            reasoning_effort="medium",
            is_preset=True,
            models=[
                ModelInfo(id="llama-3.3-70b", name="Llama 3.3 70B Local", context_window=128000),
                ModelInfo(id="qwen-2.5-coder-32b", name="Qwen 2.5 Coder 32B", context_window=128000),
            ]
        ),
        "lmstudio": ProviderConfig(
            id="lmstudio",
            name="LM Studio (Local Offline)",
            base_url="http://localhost:1234/v1",
            wire_api="chat",
            auth_type="none",
            auth_token=None,
            default_model="local-model",
            reasoning_effort="medium",
            is_preset=True,
            models=[
                ModelInfo(id="local-model", name="Active Loaded Model", context_window=128000),
            ]
        ),
    }
