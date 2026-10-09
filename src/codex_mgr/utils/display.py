"""Terminal presentation and keyboard navigation with plain-output fallback."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

from InquirerPy import inquirer
from rich import box
from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text
from rich.theme import Theme

from codex_mgr import __version__
from codex_mgr.utils.paths import get_cmgr_settings_path

CUSTOM_THEME = Theme(
    {
        "accent": "cyan",
        "openai": "green",
        "router": "magenta",
        "local": "yellow",
        "success": "green",
        "warning": "yellow",
        "error": "red",
        "muted": "bright_black",
        "border": "bright_black",
    }
)


def supports_unicode() -> bool:
    if os.environ.get("CMGR_ASCII_MODE") == "1":
        return False
    try:
        raw = get_cmgr_settings_path().read_text(encoding="utf-8")
        if json.loads(raw).get("preferences", {}).get("ascii_mode"):
            return False
    except (OSError, ValueError, AttributeError):
        pass
    return "utf" in (getattr(sys.stdout, "encoding", "") or "").lower()


console = Console(theme=CUSTOM_THEME)
_SYMBOLS = {
    "rocket": ("→", "->"),
    "check": ("✓", "[OK]"),
    "cross": ("✗", "[X]"),
    "warn": ("!", "!"),
    "arrow": ("→", "->"),
    "cursor": ("›", ">"),
    "dot": ("●", "*"),
    "gear": ("+", "+"),
    "lock": ("#", "#"),
}


def get_symbol(name: str) -> str:
    pair = _SYMBOLS.get(name, ("", ""))
    return pair[0] if supports_unicode() else pair[1]


def print_success(message: str) -> None:
    console.print(f"[success]{get_symbol('check')} {escape(message)}[/]")


def print_warning(message: str) -> None:
    console.print(f"[warning]{get_symbol('warn')} {escape(message)}[/]")


def print_error(message: str) -> None:
    console.print(f"[error]{get_symbol('cross')} {escape(message)}[/]")


def print_banner(
    active_provider: str = "Unknown",
    active_model: str = "Unknown",
    reasoning_effort: str = "medium",
    streaming: bool = True,
    context_cap: int | None = None,
) -> None:
    context = f"{context_cap:,}" if context_cap else "Model default"
    rows = [
        Text.assemble(("Provider  ", "muted"), (active_provider, "bold accent")),
        Text.assemble(("Model     ", "muted"), active_model),
        Text.assemble(("Reasoning ", "muted"), reasoning_effort),
        Text.assemble(("Context   ", "muted"), context),
    ]
    for row in rows:
        row.no_wrap = True
        row.overflow = "ellipsis" if supports_unicode() else "crop"
    title = f"Codex Manager  v{__version__}"
    if console.width < 32:
        console.print(Text(title, style="bold accent", overflow="crop", no_wrap=True))
        console.print(Group(*rows))
    else:
        console.print(
            Panel(
                Group(*rows),
                title=Text(title, style="bold"),
                title_align="left",
                width=min(console.width, 88),
                padding=(1, 1),
                border_style="border",
                box=box.ROUNDED if supports_unicode() else box.ASCII,
            )
        )
    console.print()


def select_menu(
    message: str,
    choices: list,
    default: Any = None,
    allow_back: bool = True,
    shortcuts: dict[str, Any] | None = None,
) -> Any:
    prompt = inquirer.select(
        message=message,
        choices=choices,
        default=default,
        pointer=get_symbol("cursor"),
        instruction="Esc / b: back" if allow_back else "",
        mandatory=False,
    )
    if allow_back:

        @prompt.register_kb("b")
        @prompt.register_kb("B")
        @prompt.register_kb("escape")
        def back(event):
            prompt.status["answered"] = True
            prompt.status["result"] = None
            event.app.exit(result=None)

    for key, value in (shortcuts or {}).items():

        def bind(result):
            def handler(event):
                prompt.status["answered"] = True
                prompt.status["result"] = result
                event.app.exit(result=result)

            return handler

        prompt.register_kb(key)(bind(value))
        if key.isalpha():
            prompt.register_kb(key.upper())(bind(value))
    try:
        return prompt.execute()
    except KeyboardInterrupt:
        return None
