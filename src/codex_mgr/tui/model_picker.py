"""Interactive model management and custom model catalog.

Enables users to manage models, add custom models, set default models,
and adjust reasoning tiers for any configured provider.
"""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.panel import Panel
from rich.table import Table

from codex_mgr.config.schema import ModelInfo
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.models import probe_remote_models
from codex_mgr.core.profiles import switch_active_provider
from codex_mgr.utils.display import console, print_error, print_success, print_warning


def manage_provider_models(provider_id: str) -> None:
    """Interactive management loop for models belonging to a specific provider."""
    settings = load_settings()
    provider = settings.providers.get(provider_id)
    if not provider:
        print_error(f"Provider '{provider_id}' not found.")
        return

    while True:
        # Render current models table
        table = Table(
            title=f"Models for: {provider.name}",
            border_style="dim",
            expand=False
        )
        table.add_column("Default", justify="center", width=8)
        table.add_column("Model Slug", style="bold accent")
        table.add_column("Display Name")
        table.add_column("Context Window", justify="right")
        table.add_column("Type", justify="center")

        for m in provider.models:
            is_def = "⭐" if m.id == provider.default_model else ""
            m_type = "[dim]custom[/]" if m.is_custom else "[cyan]preset[/]"
            ctx = f"{m.context_window:,} tokens"
            table.add_row(is_def, m.id, m.name, ctx, m_type)

        console.print()
        console.print(table)
        console.print()

        action = inquirer.select(
            message=f"Manage models for {provider.name}:",
            choices=[
                Choice(value="add", name="➕ Add New Model (Manual entry or endpoint probe)"),
                Choice(value="set_default", name="⭐ Set Active Default Model"),
                Choice(value="set_effort", name=f"⚙️  Change Reasoning Effort (Current: {provider.reasoning_effort})"),
                Choice(value="remove", name="🗑️  Remove Model"),
                Choice(value="back", name="↩️  Back"),
            ],
            default="back"
        ).execute()

        if action == "back":
            break

        if action == "add":
            _add_model_flow(provider)
            save_settings(settings, create_backup=True)

        elif action == "set_default":
            if not provider.models:
                print_warning("No models registered for this provider.")
                continue
            choices = [Choice(value=m.id, name=f"{m.name} ({m.id})") for m in provider.models]
            chosen_slug = inquirer.select(
                message="Select model to set as default:",
                choices=choices,
                default=provider.default_model
            ).execute()
            provider.default_model = chosen_slug
            save_settings(settings, create_backup=True)

            # If provider is currently active, sync to config.toml
            if settings.active_provider_id == provider.id:
                switch_active_provider(provider)
            print_success(f"Default model for {provider.name} set to: {chosen_slug}")

        elif action == "set_effort":
            new_effort = inquirer.select(
                message=f"Select reasoning effort for {provider.name}:",
                choices=[
                    Choice(value="low", name="low (Faster, lighter reasoning)"),
                    Choice(value="medium", name="medium (Balanced speed & depth)"),
                    Choice(value="high", name="high (Deep reasoning)"),
                    Choice(value="xhigh", name="xhigh (Extra high reasoning)"),
                    Choice(value="max", name="max (Maximum reasoning)"),
                    Choice(value="ultra", name="ultra (Agentic delegation / maximum depth)"),
                ],
                default=provider.reasoning_effort
            ).execute()
            provider.reasoning_effort = new_effort
            save_settings(settings, create_backup=True)
            if settings.active_provider_id == provider.id:
                switch_active_provider(provider)
            print_success(f"Reasoning effort updated to: {new_effort}")

        elif action == "remove":
            if len(provider.models) <= 1:
                print_warning("Cannot remove the only model registered for this provider.")
                continue
            choices = [Choice(value=m.id, name=f"{m.name} ({m.id})") for m in provider.models if m.id != provider.default_model]
            if not choices:
                print_warning("Cannot remove the active default model. Switch default model first.")
                continue
            choices.append(Choice(value=None, name="Cancel"))
            to_remove = inquirer.select(message="Select model to remove:", choices=choices).execute()
            if to_remove:
                provider.models = [m for m in provider.models if m.id != to_remove]
                save_settings(settings, create_backup=True)
                print_success(f"Removed model '{to_remove}'.")


def _add_model_flow(provider) -> None:
    """Helper to add a model either by probing the endpoint or manual entry."""
    method = inquirer.select(
        message="How would you like to add the model?",
        choices=[
            Choice(value="probe", name="🔍 Probe provider endpoint for available models"),
            Choice(value="manual", name="✏️  Enter Model ID manually"),
        ],
        default="probe"
    ).execute()

    if method == "probe":
        console.print("[dim]Probing endpoint for available models...[/]")
        ok, probed_models, err = probe_remote_models(provider.base_url, provider.auth_token, provider.wire_api)
        if ok and probed_models:
            existing_ids = {m.id for m in provider.models}
            new_models = [m for m in probed_models if m.id not in existing_ids]
            if not new_models:
                print_warning("All probed models are already added.")
                return
            choices = [Choice(value=m, name=f"{m.name} ({m.id})") for m in new_models[:25]]
            chosen: ModelInfo = inquirer.select(
                message="Select a discovered model to add:",
                choices=choices
            ).execute()
            provider.models.append(chosen)
            print_success(f"Added model: {chosen.name} ({chosen.id})")
            return
        else:
            print_warning(f"Could not auto-detect models: {err}. Falling back to manual entry.")

    # Manual entry
    mid = inquirer.text(message="Enter the exact Model ID (e.g. claude-5-5-sonnet-20260928):").execute().strip()
    if not mid:
        return
    name = inquirer.text(message="Enter a friendly display name:", default=mid).execute().strip() or mid
    ctx_str = inquirer.text(message="Context window capacity in tokens:", default="256000").execute().strip()
    try:
        ctx = int(ctx_str)
    except ValueError:
        ctx = 256000

    new_model = ModelInfo(id=mid, name=name, context_window=ctx, is_custom=True)
    provider.models.append(new_model)
    print_success(f"Added custom model: {name} ({mid})")
