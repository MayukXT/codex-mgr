"""Supported native and owned-adapter model controls."""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.text import Text

from codex_mgr.config.settings import load_settings
from codex_mgr.core.adapters.messages_controls import EFFORTS, MODES
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
    modern = provider.wire_api == "anthropic_proxy" and provider.messages_thinking is not None
    model = model_metadata(provider, provider.default_model)
    output_default = (
        min(8192, model.max_output_tokens) if model and model.max_output_tokens else 8192
    )
    effort = provider.reasoning_effort or (
        "Not set" if provider.wire_api == "anthropic_proxy" and not modern else "Model default"
    )
    if not modern and not provider.reasoning_enabled:
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
                        else f"Adapter default ({output_default:,})"
                        if provider.wire_api == "anthropic_proxy"
                        else "Provider default"
                    ),
                    None,
                ),
                ("Streaming: " + ("On" if provider.streaming else "Off"), None),
            ]
        )
    if provider.wire_api == "anthropic_proxy":
        if modern:
            rows.insert(4, ("Thinking: " + _mode_label(provider.messages_thinking), None))
        if not modern or provider.messages_thinking == "enabled":
            rows.extend(
                [
                    (
                        "Thinking budget: "
                        + (
                            f"{provider.reasoning_budget:,}"
                            if provider.reasoning_budget
                            else "None"
                        )
                        + (
                            " (inactive)"
                            if provider.reasoning_budget
                            and (
                                modern
                                and provider.messages_thinking != "enabled"
                                or not modern
                                and not provider.reasoning_enabled
                            )
                            else ""
                        ),
                        None,
                    ),
                ]
            )
        rows.append(
            ("Summaries: " + ("Shown" if provider.messages_summary == "auto" else "Hidden"), None)
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


def _mode_label(mode):
    return {
        None: "Legacy budget controls",
        "adaptive": "Adaptive",
        "enabled": "Manual budget",
        "disabled": "Off",
        "between_tools": "Between tools",
    }[mode]


def _choose_mode(provider):
    model = model_metadata(provider, provider.default_model)
    legacy = object()
    choices = [Choice(legacy, _mode_label(None))]
    for mode in MODES:
        supported = model.thinking_types.get(mode) if model else None
        if supported is not False:
            choices.append(
                Choice(mode, _mode_label(mode) + (" (unverified)" if supported is None else ""))
            )
    choices.append(Choice(None, "Back"))
    current = provider.messages_thinking
    value = select_menu(
        "Thinking mode",
        choices,
        default=current
        if current and any(choice.value == current for choice in choices)
        else legacy,
        allow_back=True,
    )
    if value is not None:
        provider.messages_thinking = None if value is legacy else value


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
        if provider.wire_api == "anthropic_proxy":
            choices.append(Choice("thinking", "Thinking mode"))
        if provider.wire_api in {"chat", "anthropic_proxy"}:
            choices.extend([Choice("output", "Output limit"), Choice("streaming", "Streaming")])
        if provider.wire_api == "anthropic_proxy":
            if provider.messages_thinking in {None, "enabled"}:
                choices.append(Choice("budget", "Thinking budget"))
            choices.append(Choice("summary", "Summaries"))
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
        elif action == "thinking":
            _choose_mode(provider)
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
            modern = (
                provider.wire_api == "anthropic_proxy" and provider.messages_thinking is not None
            )
            if modern:
                levels = [level for level in (levels or EFFORTS) if level in EFFORTS]
                if model and model.effort_supported is False:
                    levels = []
            if levels or modern:
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
            if not modern:
                provider.reasoning_enabled = effort != "none"
        elif action == "save":
            ok, message = save_model_settings(settings, selected)
            (print_success if ok else print_error)(message)
            if ok:
                return
