"""Interactive session picker and auto-bridge launcher.

Displays human-friendly conversation titles, origin badges, and handles
cross-provider conversion before launching Codex seamlessly.
"""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.panel import Panel

from codex_mgr.config.settings import load_settings
from codex_mgr.core.bridge import bridge_session_rollout
from codex_mgr.core.runner import is_codex_running, launch_codex
from codex_mgr.core.sessions import SessionItem, list_sessions
from codex_mgr.utils.display import (
    console,
    get_symbol,
    print_error,
    print_success,
    print_warning,
    select_menu,
)


def interactive_resume_session(target_provider_id: str | None = None) -> None:
    """Show interactive session picker with auto-bridge validation and handoff."""
    settings = load_settings()
    active_id = target_provider_id or settings.active_provider_id

    # Check for active codex running lock
    if is_codex_running():
        print_warning("Codex CLI is currently running in another window.")
        confirm_proceed = inquirer.confirm(
            message="Switching or modifying sessions while Codex is active may cause file lock issues. Continue anyway?",
            default=False,
        ).execute()
        if not confirm_proceed:
            return

    with console.status("[accent]Scanning conversation sessions...", spinner="dots"):
        sessions = list_sessions(limit=40)

    if not sessions:
        print_warning("No previous conversation sessions found in ~/.codex/sessions.")
        return

    choices = []
    for s in sessions:
        needs_conv = s.origin_provider != active_id
        action_badge = f"[Auto-Bridge ➔ {active_id}]" if needs_conv else "[Direct Resume]"
        label = f"[{s.origin_provider.upper():<10}] {s.thread_name[:38]:<38} ({s.relative_time}) {action_badge}"
        choices.append(Choice(value=s, name=label))

    choices.append(Choice(value=None, name="← Back (b)"))

    selected_session: SessionItem | None = select_menu(
        message="Select session to resume:",
        choices=choices,
        default=choices[0].value if choices else None,
        allow_back=True,
    )

    if selected_session is None:
        return

    try:
        selected_session.require_rollout()
    except (OSError, ValueError) as exc:
        print_error(str(exc))
        return

    # Check if conversion is needed
    if selected_session.origin_provider != active_id:
        panel_content = (
            f"This session was originally bound to: [bold router]{selected_session.origin_provider.upper()}[/]\n"
            f"Your current target provider is     : [bold openai]{active_id.upper()}[/]\n\n"
            f"• An immutable backup snapshot will be saved to ~/.codex/backups/\n"
            f"• Incompatible encrypted reasoning blobs will be cleanly bridged\n"
            f"• 100% of your messages, prompts, code diffs, and tool calls are preserved."
        )
        console.print(
            Panel(
                panel_content, title="⚡ Cross-Provider Auto-Bridge Notice", border_style="warning"
            )
        )

        confirm = inquirer.confirm(
            message=f"Proceed with auto-bridge and launch Codex under '{active_id}'?", default=True
        ).execute()

        if not confirm:
            print_warning("Resumption cancelled by user.")
            return

        if selected_session.rollout_path and selected_session.rollout_path.exists():
            with console.status("[accent]Bridging session reasoning blobs...", spinner="dots"):
                ok, _count, msg = bridge_session_rollout(
                    selected_session.rollout_path,
                    target_provider_id=active_id,
                    create_backup_snapshot=True,
                )
            if ok:
                print_success(msg)
            else:
                print_error(f"Bridge failed: {msg}")
                return

    # Process handoff to Codex
    console.print(
        f"\n[bold accent]{get_symbol('rocket')} Launching Codex session: {selected_session.thread_name}[/]\n"
    )
    launch_codex(session_id=selected_session.id)
