"""Rich visual formatting, theme tokens, and terminal capability degradation.

Ensures beautiful styling on modern terminals with graceful ASCII fallback
on older Windows Command Prompt shells without garbled characters.
"""

from __future__ import annotations

import os
import sys

# Ensure UTF-8 output on Windows consoles
if sys.platform == "win32":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from rich.console import Console
from rich.panel import Panel
from rich.theme import Theme

# Custom cohesive dark theme palette
CUSTOM_THEME = Theme({
    "accent": "#339CFF",
    "openai": "#10A37F",
    "router": "#9333EA",
    "local": "#F59E0B",
    "success": "#10B981",
    "warning": "#FBBF24",
    "error": "#EF4444",
    "muted": "#9CA3AF",
    "border": "#374151",
})

console = Console(theme=CUSTOM_THEME, safe_box=True)


def supports_unicode() -> bool:
    """Check if the current terminal supports full UTF-8 Unicode characters."""
    encoding = getattr(sys.stdout, "encoding", "") or ""
    if "utf" not in encoding.lower():
        return False
    if os.environ.get("CMGR_ASCII_MODE") == "1":
        return False
    return True


# Dual symbol table: Unicode vs ASCII fallback
SYMBOLS = {
    "rocket": "🚀" if supports_unicode() else "[*]",
    "check": "✓" if supports_unicode() else "[OK]",
    "cross": "✗" if supports_unicode() else "[X]",
    "warn": "⚠" if supports_unicode() else "[!]",
    "arrow": "➔" if supports_unicode() else "->",
    "cursor": "❯" if supports_unicode() else ">",
    "dot": "●" if supports_unicode() else "*",
    "gear": "⚙" if supports_unicode() else "[+]",
    "lock": "🔒" if supports_unicode() else "[#]",
}


def get_symbol(name: str) -> str:
    """Return a symbol formatted for the current terminal's capabilities."""
    return SYMBOLS.get(name, "")


def print_success(message: str) -> None:
    """Print a success confirmation message."""
    sym = get_symbol("check")
    console.print(f"[bold success]{sym} {message}[/]")


def print_warning(message: str) -> None:
    """Print a warning notification message."""
    sym = get_symbol("warn")
    console.print(f"[bold warning]{sym} {message}[/]")


def print_error(message: str) -> None:
    """Print an error message."""
    sym = get_symbol("cross")
    console.print(f"[bold error]{sym} {message}[/]")


def print_banner(active_provider: str = "Unknown", active_model: str = "Unknown") -> None:
    """Render the high-polish main banner."""
    header = (
        f"[bold accent]CODEX MANAGER[/] [dim](cmgr)[/] [muted]v1.0.0[/]\n"
        f"[dim]Universal Multi-Provider Bridge & Session Manager for Codex[/]\n\n"
        f"Active Provider: [bold openai]{active_provider}[/]  •  "
        f"Default Model: [bold accent]{active_model}[/]"
    )
    console.print(Panel(header, border_style="accent", expand=False))
