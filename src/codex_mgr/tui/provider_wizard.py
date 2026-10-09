"""6-Step chronological guided provider onboarding wizard.

Empowers non-technical users to configure any model provider endpoint,
probe available models with zero token spend, and test connection health.
"""

from __future__ import annotations

from typing import Optional

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.panel import Panel

from codex_mgr.config.schema import ModelInfo, ProviderConfig
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.models import probe_remote_models, test_provider_connectivity
from codex_mgr.core.profiles import switch_active_provider
from codex_mgr.core.providers import get_default_presets
from codex_mgr.utils.display import console, get_symbol, print_error, print_success, print_warning
from codex_mgr.utils.normalizer import normalize_base_url, sanitize_api_key


def run_provider_wizard() -> Optional[ProviderConfig]:
    """Execute the chronological 6-step provider configuration wizard."""
    console.print(Panel(
        "[bold accent]Configure a New AI Model Provider[/]\n"
        "Connect OpenAI, Anthropic Claude 5.5, OpenRouter, DeepSeek, or local models in 6 simple steps.",
        border_style="accent"
    ))

    presets = get_default_presets()

    # Step 1: Template selection
    template_choices = [
        Choice(value="openrouter", name="OpenRouter (Universal Gateway: Claude 5.5, GPT-6, DeepSeek)"),
        Choice(value="anthropic", name="Anthropic (Direct Claude 5.5: Sonnet, Opus, Haiku, Fable)"),
        Choice(value="agentrouter", name="AgentRouter (High-Speed Multi-Model Gateway)"),
        Choice(value="deepseek", name="DeepSeek (Official DeepSeek R2 / V3)"),
        Choice(value="groq", name="Groq (Ultra-Fast LPU Inference)"),
        Choice(value="gemini", name="Google Gemini (OpenAI Compatible)"),
        Choice(value="ollama", name="Ollama (Local Offline Self-Hosted)"),
        Choice(value="lmstudio", name="LM Studio (Desktop Local Model Runner)"),
        Choice(value="custom", name="Custom Provider (Any OpenAI or Anthropic compatible URL)"),
    ]

    selected_template = inquirer.select(
        message="[Step 1/6] Choose a provider template:",
        choices=template_choices,
        default="openrouter"
    ).execute()

    template_cfg = presets.get(selected_template)

    # Step 2: Name and Alias
    default_name = template_cfg.name if template_cfg else "My Custom Provider"
    provider_name = inquirer.text(
        message="[Step 2/6] Enter a friendly name for this provider:",
        default=default_name
    ).execute().strip() or default_name

    default_id = selected_template if selected_template != "custom" else provider_name.lower().replace(" ", "_")
    provider_id = inquirer.text(
        message="Enter a short identifier slug:",
        default=default_id
    ).execute().strip() or default_id

    # Step 3: Base URL & Wire API
    default_url = template_cfg.base_url if template_cfg else "https://api.example.com/v1"
    raw_url = inquirer.text(
        message="[Step 3/6] Enter the provider Base URL:",
        default=default_url
    ).execute().strip()

    wire_api = template_cfg.wire_api if template_cfg else "responses"
    if selected_template == "custom":
        wire_api = inquirer.select(
            message="Select the wire protocol:",
            choices=[
                Choice(value="responses", name="responses (Codex Native Responses API - Recommended)"),
                Choice(value="chat", name="chat (Standard OpenAI /v1/chat/completions)"),
            ],
            default="responses"
        ).execute()

    clean_url = normalize_base_url(raw_url, wire_api)

    # Step 4: Authentication
    auth_token = None
    if selected_template not in ("ollama", "lmstudio"):
        auth_mode = inquirer.select(
            message="[Step 4/6] How would you like to provide the API key?",
            choices=[
                Choice(value="paste", name="Paste API key now (saved securely in ~/.codex/cmgr.json)"),
                Choice(value="env", name="Reference an environment variable (e.g. $OPENROUTER_KEY)"),
            ],
            default="paste"
        ).execute()

        if auth_mode == "paste":
            raw_key = inquirer.secret(
                message="Enter your API Key / Bearer Token:"
            ).execute()
            auth_token = sanitize_api_key(raw_key)
        else:
            default_env = f"${provider_id.upper()}_API_KEY"
            env_var = inquirer.text(
                message="Enter the environment variable name:",
                default=default_env
            ).execute().strip()
            auth_token = env_var if env_var.startswith("$") else f"${env_var}"

    # Step 5: Zero-Token Model Discovery
    console.print("\n[dim]Probing endpoint for available models (zero token cost)...[/]")
    ok, probed_models, err = probe_remote_models(clean_url, auth_token, wire_api)

    models_list = []
    default_model = template_cfg.default_model if template_cfg else "default-model"

    if ok and probed_models:
        print_success(f"Discovered {len(probed_models)} available models on the endpoint!")
        model_choices = [Choice(value=m.id, name=f"{m.name} ({m.id})") for m in probed_models[:20]]
        model_choices.append(Choice(value="__custom__", name="[+] Enter another model ID manually..."))

        selected_model_slug = inquirer.select(
            message="[Step 5/6] Select the default model:",
            choices=model_choices,
            default=model_choices[0].value
        ).execute()

        if selected_model_slug == "__custom__":
            custom_mid = inquirer.text(message="Enter the Model ID:").execute().strip()
            default_model = custom_mid
            models_list.append(ModelInfo(id=custom_mid, name=custom_mid, is_custom=True))
        else:
            default_model = selected_model_slug
            models_list = probed_models
    else:
        if err:
            print_warning(f"Note: {err}")
        custom_mid = inquirer.text(
            message="[Step 5/6] Enter the default Model ID to use:",
            default=default_model
        ).execute().strip() or default_model
        default_model = custom_mid
        models_list.append(ModelInfo(id=custom_mid, name=custom_mid, is_custom=True))

    reasoning_effort = inquirer.select(
        message="Select default reasoning effort:",
        choices=[
            Choice(value="low", name="low (Faster, lighter thinking)"),
            Choice(value="medium", name="medium (Balanced speed & depth)"),
            Choice(value="high", name="high (Deep reasoning for complex code)"),
            Choice(value="max", name="max (Maximum reasoning depth)"),
            Choice(value="ultra", name="ultra (Agentic delegation / maximum deliberation)"),
        ],
        default="medium"
    ).execute()

    # Step 6: Connection Health Check & Activation
    new_provider = ProviderConfig(
        id=provider_id,
        name=provider_name,
        base_url=clean_url,
        wire_api=wire_api,
        auth_type="none" if selected_template in ("ollama", "lmstudio") else "bearer_token",
        auth_token=auth_token,
        default_model=default_model,
        reasoning_effort=reasoning_effort,
        is_preset=False,
        models=models_list
    )

    console.print("\n[dim]Performing final reachability handshake...[/]")
    is_healthy, status_msg, latency = test_provider_connectivity(new_provider)

    if is_healthy:
        print_success(f"[Step 6/6] Health Check PASSED: {status_msg} ({latency}ms)")
    else:
        print_warning(f"[Step 6/6] Notice: {status_msg}")

    # Save to settings
    settings = load_settings()
    settings.providers[provider_id] = new_provider
    save_settings(settings, create_backup=True)
    print_success(f"Provider '{provider_name}' saved successfully to ~/.codex/cmgr.json!")

    # Prompt to activate
    activate_now = inquirer.confirm(
        message="Would you like to switch to this provider right now?",
        default=True
    ).execute()

    if activate_now:
        settings.active_provider_id = provider_id
        save_settings(settings, create_backup=False)
        switch_ok, switch_msg = switch_active_provider(new_provider)
        if switch_ok:
            print_success(f"Active provider updated: {switch_msg}")
        else:
            print_error(f"Failed to switch in config.toml: {switch_msg}")

    return new_provider
