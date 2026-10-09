"""Interactive model management and custom model catalog.

Enables users to manage models, add custom models, set default models,
and adjust reasoning tiers for any configured provider.
"""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.table import Table

from codex_mgr.config.schema import ModelInfo
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.activation import apply_settings
from codex_mgr.core.models import probe_remote_models
from codex_mgr.utils.display import console, print_error, print_success, print_warning, select_menu


def manage_provider_models(provider_id: str) -> None:
    """Interactive management loop for models belonging to a specific provider."""
    settings = load_settings()
    provider = settings.providers.get(provider_id)
    if not provider:
        print_error(f"Provider '{provider_id}' not found.")
        return

    while True:
        console.clear()
        # Render current models table
        table = Table(title=f"Models for: {provider.name}", border_style="#475569", expand=False)
        table.add_column("Default", justify="center", width=8)
        table.add_column("Model Slug", style="bold cyan")
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

        action = select_menu(
            message=f"Manage models for {provider.name}:",
            choices=[
                Choice(value="set_default", name="1. ⭐  Set Default Model"),
                Choice(value="add", name="2. ➕  Add New Model"),
                Choice(value="remove", name="3. 🗑️   Remove Model"),
                Choice(value="tuning", name="4. 🔧  Advanced Tuning"),
                Choice(value=None, name="← Back (b)"),
            ],
            default="set_default",
            allow_back=True,
            shortcuts={"1": "set_default", "2": "add", "3": "remove", "4": "tuning"},
        )

        if not action:
            break

        if action == "tuning":
            from codex_mgr.tui.advanced_controls import run_advanced_controls

            run_advanced_controls(provider.id)
            settings = load_settings()
            provider = settings.providers.get(provider_id)
            continue

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
                default=provider.default_model,
            ).execute()
            provider.default_model = chosen_slug
            ok, message = apply_settings(
                settings,
                activate_provider_id=provider.id
                if settings.active_provider_id == provider.id
                else None,
            )
            (print_success if ok else print_error)(message)
            settings = load_settings()
            provider = settings.providers[provider_id]

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
                default=provider.reasoning_effort,
            ).execute()
            provider.reasoning_effort = new_effort
            ok, message = apply_settings(
                settings,
                activate_provider_id=provider.id
                if settings.active_provider_id == provider.id
                else None,
            )
            (print_success if ok else print_error)(message)

        elif action == "remove":
            if len(provider.models) <= 1:
                print_warning("Cannot remove the only model registered for this provider.")
                continue
            choices = [
                Choice(value=m.id, name=f"{m.name} ({m.id})")
                for m in provider.models
                if m.id != provider.default_model
            ]
            if not choices:
                print_warning("Cannot remove the active default model. Switch default model first.")
                continue
            choices.append(Choice(value=None, name="Cancel"))
            to_remove = inquirer.select(
                message="Select model to remove:", choices=choices
            ).execute()
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
        default="probe",
    ).execute()

    if method == "probe":
        console.print("[dim]Probing endpoint for available models...[/]")
        ok, probed_models, err = probe_remote_models(
            provider.base_url, provider.auth_token, provider.wire_api
        )
        if ok and probed_models:
            existing_ids = {m.id for m in provider.models}
            new_models = [m for m in probed_models if m.id not in existing_ids]
            if not new_models:
                print_warning("All probed models are already added.")
                return
            choices = [Choice(value=m, name=f"{m.name} ({m.id})") for m in new_models[:25]]
            chosen: ModelInfo = inquirer.select(
                message="Select a discovered model to add:", choices=choices
            ).execute()
            provider.models.append(chosen)
            print_success(f"Added model: {chosen.name} ({chosen.id})")
            return
        else:
            print_warning(f"Could not auto-detect models: {err}. Falling back to manual entry.")

    # Manual entry
    mid = (
        inquirer.text(message="Enter the exact Model ID (e.g. claude-5-5-sonnet-20260928):")
        .execute()
        .strip()
    )
    if not mid:
        return
    name = (
        inquirer.text(message="Enter a friendly display name:", default=mid).execute().strip()
        or mid
    )
    ctx_str = (
        inquirer.text(message="Context window capacity in tokens:", default="256000")
        .execute()
        .strip()
    )
    try:
        ctx = int(ctx_str)
    except ValueError:
        ctx = 256000

    new_model = ModelInfo(id=mid, name=name, context_window=ctx, is_custom=True)
    provider.models.append(new_model)
    print_success(f"Added custom model: {name} ({mid})")
