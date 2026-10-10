"""Model controls with a verified Codex configuration effect."""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.markup import escape

from codex_mgr.config.settings import load_settings
from codex_mgr.core.activation import apply_settings
from codex_mgr.core.models import model_metadata
from codex_mgr.utils.display import console, print_error, print_success, select_menu


def run_advanced_controls(provider_id: str | None = None) -> None:
    settings = load_settings()
    selected = provider_id or settings.active_provider_id
    provider = settings.providers.get(selected)
    if provider is None:
        print_error("Provider not found.")
        return
    provider = provider.model_copy(deep=True)
    settings.providers[selected] = provider
    while True:
        console.print(f"\n[bold]{escape(provider.name)}[/] / {escape(provider.default_model)}")
        context = (
            f"{provider.context_window_limit:,}"
            if provider.context_window_limit
            else "Model default"
        )
        console.print(
            f"Context: {context}    Reasoning: {escape(provider.reasoning_effort or 'Model default')}\n"
        )
        action = select_menu(
            message="Model settings",
            choices=[
                Choice("context", name="Context limit"),
                Choice("effort", name="Reasoning effort"),
                Choice("save", name="Save"),
                Choice(None, name="Back"),
            ],
            allow_back=True,
        )
        if action is None:
            return
        if action == "context":
            value = (
                inquirer.text(
                    message="Token limit (blank for model default)",
                    default=str(provider.context_window_limit or ""),
                )
                .execute()
                .strip()
            )
            try:
                if value and int(value) <= 0:
                    raise ValueError
                provider.context_window_limit = int(value) if value else None
            except ValueError:
                print_error("Enter a positive integer or leave blank.")
        elif action == "effort":
            model = model_metadata(provider, provider.default_model)
            levels = model.supported_reasoning_levels if model else []
            if levels:
                model_default = object()
                effort = select_menu(
                    "Reasoning effort",
                    [
                        Choice(model_default, "Model default"),
                        *[Choice(level, level) for level in levels],
                        Choice(None, "Back"),
                    ],
                    default=provider.reasoning_effort
                    if provider.reasoning_effort in levels
                    else model_default,
                    allow_back=True,
                )
                if effort is None:
                    continue
                if effort is model_default:
                    effort = ""
            else:
                effort = (
                    inquirer.text(
                        message="Provider-supported reasoning value",
                        default=provider.reasoning_effort or "",
                    )
                    .execute()
                    .strip()
                )
            provider.reasoning_effort = effort or None
            provider.reasoning_enabled = effort != "none"
        elif action == "save":
            ok, message = apply_settings(
                settings,
                activate_provider_id=selected if selected == settings.active_provider_id else None,
            )
            (print_success if ok else print_error)(message)
            if ok:
                return
