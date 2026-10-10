"""Supported native and owned-adapter model controls."""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.text import Text

from codex_mgr.config.settings import load_settings
from codex_mgr.core.models import model_metadata, save_model_settings
from codex_mgr.utils.display import (
    console,
    print_error,
    print_success,
    select_menu,
    single_line,
    supports_unicode,
)


def _show_settings(provider):
    adapter = provider.wire_api in {"chat", "anthropic_proxy"}
    effort = provider.reasoning_effort or (
        "Not set" if provider.wire_api == "anthropic_proxy" else "Model default"
    )
    if not provider.reasoning_enabled:
        effort = "Off"
    rows = [
        (provider.name, "bold"),
        (provider.default_model, "muted"),
        ("", None),
        (
            "Context: "
            + (
                f"{provider.context_window_limit:,}"
                if provider.context_window_limit
                else "Model default"
            ),
            None,
        ),
        ("Effort: " + effort, None),
    ]
    if adapter:
        rows.extend(
            [
                (
                    "Output: "
                    + (
                        f"{provider.max_output_tokens:,}"
                        if provider.max_output_tokens
                        else "Adapter default (8,192)"
                        if provider.wire_api == "anthropic_proxy"
                        else "Provider default"
                    ),
                    None,
                ),
                ("Streaming: " + ("On" if provider.streaming else "Off"), None),
            ]
        )
    if provider.wire_api == "anthropic_proxy":
        rows.extend(
            [
                (
                    "Thinking budget: "
                    + (f"{provider.reasoning_budget:,}" if provider.reasoning_budget else "None")
                    + (
                        " (inactive)"
                        if provider.reasoning_budget and not provider.reasoning_enabled
                        else ""
                    ),
                    None,
                ),
                (
                    "Summaries: " + ("Shown" if provider.messages_summary == "auto" else "Hidden"),
                    None,
                ),
            ]
        )
    console.print()
    overflow = "ellipsis" if supports_unicode() else "crop"
    for value, style in rows:
        console.print(
            Text(
                single_line(value),
                style=style,
                no_wrap=True,
                overflow=overflow,
            ),
            no_wrap=True,
            overflow=overflow,
        )
    console.print()


def _token_limit(provider, field, message):
    value = (
        inquirer.text(message=message, default=str(getattr(provider, field) or ""))
        .execute()
        .strip()
    )
    try:
        parsed = int(value) if value else None
        if parsed is not None and parsed <= 0:
            raise ValueError
    except ValueError:
        print_error("Enter a positive integer or leave blank.")
        return
    setattr(provider, field, parsed)


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
        _show_settings(provider)
        choices = [
            Choice("context", name="Context limit"),
            Choice("effort", name="Reasoning effort"),
        ]
        if provider.wire_api in {"chat", "anthropic_proxy"}:
            choices.extend([Choice("output", "Output limit"), Choice("streaming", "Streaming")])
        if provider.wire_api == "anthropic_proxy":
            choices.extend([Choice("budget", "Thinking budget"), Choice("summary", "Summaries")])
        choices.extend([Choice("save", "Save"), Choice(None, "Back")])
        action = select_menu(
            message="Model settings",
            choices=choices,
            allow_back=True,
        )
        if action is None:
            return
        if action == "context":
            _token_limit(provider, "context_window_limit", "Token limit (blank for model default)")
        elif action == "output":
            _token_limit(provider, "max_output_tokens", "Output limit (blank for default)")
        elif action == "budget":
            _token_limit(provider, "reasoning_budget", "Thinking tokens (blank for no budget)")
        elif action == "streaming":
            value = select_menu(
                "Streaming",
                [Choice(True, "On"), Choice(False, "Off"), Choice(None, "Back")],
                default=provider.streaming,
                allow_back=True,
            )
            if value is not None:
                provider.streaming = value
        elif action == "summary":
            value = select_menu(
                "Supplied summaries",
                [Choice("none", "Hidden"), Choice("auto", "Shown"), Choice(None, "Back")],
                default=provider.messages_summary,
                allow_back=True,
            )
            if value is not None:
                provider.messages_summary = value
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
            ok, message = save_model_settings(settings, selected)
            (print_success if ok else print_error)(message)
            if ok:
                return
