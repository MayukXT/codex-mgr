"""Searchable model catalog with details and guarded active-model changes."""

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from codex_mgr.config.settings import load_settings
from codex_mgr.core.activation import apply_settings
from codex_mgr.core.models import catalog_is_fresh, model_metadata, refresh_provider_models
from codex_mgr.tui.provider_wizard import choose_model
from codex_mgr.utils.display import console, print_error, print_success, print_warning, select_menu


def manage_provider_models(provider_id):
    while True:
        settings = load_settings()
        provider = settings.providers.get(provider_id)
        if provider is None:
            print_error("Connection not found.")
            return
        console.print("\n" + provider.name, markup=False)
        console.print(
            "Model: " + (provider.default_model if provider.model_selected else "Choose a model"),
            markup=False,
        )
        console.print(
            f"Catalog: {'fresh' if catalog_is_fresh(provider) else 'unverified or stale'}; {len(provider.models)} entries\n",
            markup=False,
        )
        action = select_menu(
            "Models:",
            [
                Choice("select", "Select model"),
                Choice("refresh", "Refresh catalog"),
                Choice("details", "Model details"),
                Choice("remove", "Remove model"),
                Choice("tuning", "Model settings"),
                Choice(None, "Back"),
            ],
        )
        if not action:
            return
        if action == "tuning":
            from codex_mgr.tui.advanced_controls import run_advanced_controls

            run_advanced_controls(provider_id)
            continue
        if action == "select":
            if not choose_model(provider):
                continue
        elif action == "refresh":
            ok, _, error = refresh_provider_models(provider)
            if not ok:
                print_warning(error or "Catalog unavailable.")
                continue
        else:
            choices = [Choice(m.id, m.name) for m in provider.models] + [Choice(None, "Back")]
            selected = inquirer.fuzzy(
                message="Model:", choices=choices, mandatory=False, max_height="60%"
            ).execute()
            if not selected:
                continue
            model = model_metadata(provider, selected)
            if action == "details":
                context = f"{model.context_window:,}" if model.context_window else "Unknown"
                console.print(
                    f"ID: {model.id}\nName: {model.name}\nContext: {context}\nReasoning: {', '.join(model.supported_reasoning_levels) or 'Unknown'}\nSource: {'manual' if model.is_custom else provider.catalog_source or 'unverified'}",
                    markup=False,
                )
                inquirer.text(message="Enter to return:").execute()
                continue
            if selected == provider.default_model:
                print_warning("Choose another default before removing this model.")
                continue
            provider.models = [m for m in provider.models if m.id != selected]
        ok, message = apply_settings(
            settings,
            activate_provider_id=provider_id
            if settings.active_provider_id == provider_id and action == "select"
            else None,
        )
        (print_success if ok else print_error)(message)
