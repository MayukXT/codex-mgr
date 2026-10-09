"""Main interactive TUI dashboard for codex-mgr.

Presents an elegant, arrow-key navigable dashboard that coordinates
session resumption, provider switching, models, backups, and health checks.
"""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.panel import Panel

from codex_mgr.config.settings import load_settings
from codex_mgr.core.activation import activate_provider
from codex_mgr.core.backup import list_session_backups, restore_session_backup
from codex_mgr.core.runner import launch_codex
from codex_mgr.core.sessions import list_sessions
from codex_mgr.tui.doctor_view import render_doctor_report
from codex_mgr.tui.model_picker import manage_provider_models
from codex_mgr.tui.portable_view import run_portable_menu
from codex_mgr.tui.provider_wizard import run_provider_wizard
from codex_mgr.tui.session_picker import interactive_resume_session
from codex_mgr.utils.display import (
    console,
    get_symbol,
    print_banner,
    print_error,
    print_success,
    print_warning,
    select_menu,
)


def run_main_dashboard() -> None:
    """Run the primary interactive loop."""
    while True:
        settings = load_settings()
        active_p = settings.providers.get(settings.active_provider_id)
        active_name = active_p.name if active_p else settings.active_provider_id
        active_model = (
            active_p.default_model if active_p and active_p.model_selected else "Codex default"
        )
        active_effort = active_p.reasoning_effort if active_p else "medium"
        active_stream = active_p.streaming if active_p else True
        active_ctx = active_p.context_window_limit if active_p else None

        console.clear()
        print_banner(
            active_provider=active_name,
            active_model=active_model,
            reasoning_effort=active_effort,
            streaming=active_stream,
            context_cap=active_ctx,
        )

        action = select_menu(
            message="Select action (press 1-6 or 'q' to quit):",
            choices=[
                Choice(value="resume", name="1. Resume session"),
                Choice(value="new", name="2. New session"),
                Choice(value="switch", name="3. Connection and model"),
                Choice(value="manage", name="4. Manage connections"),
                Choice(value="tuning", name="5. Model settings"),
                Choice(value="tools", name="6. Tools"),
                Choice(value="exit", name="q. Exit"),
            ],
            default="resume",
            allow_back=False,
            shortcuts={
                "1": "resume",
                "2": "new",
                "3": "switch",
                "4": "manage",
                "5": "tuning",
                "6": "tools",
                "q": "exit",
            },
        )

        if not action or action == "exit":
            console.print("[dim]Goodbye![/]")
            break

        elif action == "resume":
            interactive_resume_session()

        elif action == "new":
            console.print(
                f"\n[bold accent]{get_symbol('rocket')} Launching fresh Codex session with {active_name}...[/]\n"
            )
            launch_codex()

        elif action == "switch":
            _handle_switch_provider()

        elif action == "manage":
            _handle_manage_providers()

        elif action == "tuning":
            from codex_mgr.tui.advanced_controls import run_advanced_controls

            run_advanced_controls()

        elif action == "tools":
            _handle_diagnostics_tools()


def _handle_diagnostics_tools() -> None:
    """Submenu for diagnostic checks, backups, updates, and portability."""
    while True:
        action = select_menu(
            message="Diagnostics & Tools:",
            choices=[
                Choice(value="doctor", name="1. 🩺  Doctor & Diagnostics"),
                Choice(value="revert", name="2. 🔄  Revert Session Snapshot"),
                Choice(value="port", name="3. 📦  Export & Import Setup"),
                Choice(value="update", name="4. ✨  Version & Updates"),
                Choice(value=None, name="← Back (b)"),
            ],
            default="doctor",
            allow_back=True,
            shortcuts={"1": "doctor", "2": "revert", "3": "port", "4": "update"},
        )

        if not action:
            break

        if action == "doctor":
            render_doctor_report()
            inquirer.text(message="Press [Enter] to return...").execute()
        elif action == "revert":
            _handle_revert_session()
        elif action == "port":
            run_portable_menu()
        elif action == "update":
            _handle_version_update()


def _handle_switch_provider() -> None:
    """Prompt to switch active provider."""
    settings = load_settings()
    choices = []

    for pid, p in settings.providers.items():
        is_active = pid == settings.active_provider_id
        mark = "● " if is_active else "  "
        choices.append(Choice(value=pid, name=f"{mark}{p.name}"))

    choices.append(Choice(value=None, name="← Back (b)"))

    chosen_pid = select_menu(
        message="Select provider to activate:",
        choices=choices,
        default=settings.active_provider_id,
        allow_back=True,
    )

    if chosen_pid and chosen_pid != settings.active_provider_id:
        ok, msg = activate_provider(chosen_pid)
        if ok:
            print_success(msg)
        else:
            print_error(msg)
        inquirer.text(message="Press [Enter] to continue...").execute()
    if chosen_pid:
        manage_provider_models(chosen_pid)


def _handle_manage_providers() -> None:
    """Submenu listing only provider names and nothing else."""
    while True:
        settings = load_settings()
        choices = []
        for pid, p in settings.providers.items():
            choices.append(Choice(value=pid, name=p.name))
        choices.append(Choice(value="__new__", name="[+] Add New Provider"))
        choices.append(Choice(value=None, name="← Back (b)"))

        chosen = select_menu(
            message="Select provider to configure:",
            choices=choices,
            allow_back=True,
        )

        if not chosen:
            break

        if chosen == "__new__":
            run_provider_wizard()
        else:
            from codex_mgr.core.models import test_provider_connectivity
            from codex_mgr.core.providers import delete_provider

            action = select_menu(
                "Connection:",
                [
                    Choice("models", "Models"),
                    Choice("edit", "Edit"),
                    Choice("test", "Test catalog access"),
                    Choice("delete", "Delete"),
                    Choice(None, "Back"),
                ],
            )
            if action == "models":
                manage_provider_models(chosen)
            elif action == "edit":
                run_provider_wizard(chosen)
            elif action == "test":
                ok, message, elapsed = test_provider_connectivity(settings.providers[chosen])
                (print_success if ok else print_error)(f"{message} ({elapsed} ms)")
                inquirer.text(message="Enter to return:").execute()
            elif (
                action == "delete"
                and inquirer.confirm(message="Delete this connection?", default=False).execute()
            ):
                ok, message = delete_provider(settings, chosen)
                (print_success if ok else print_error)(message)


def _handle_revert_session() -> None:
    """Interactive rollback of bridged session."""
    sessions = list_sessions(limit=40)
    revertable = []

    for s in sessions:
        backups = list_session_backups(s.id)
        if backups:
            revertable.append((s, backups))

    if not revertable:
        print_warning("No session backups found in ~/.codex/backups/sessions/.")
        inquirer.text(message="Press [Enter] to return...").execute()
        return

    choices = [
        Choice(value=item, name=f"{item[0].thread_name[:40]} ({len(item[1])} backups available)")
        for item in revertable
    ]
    choices.append(Choice(value=None, name="← Back (b)"))

    chosen_pair = select_menu(
        message="Select session to restore from backup:",
        choices=choices,
        allow_back=True,
    )

    if not chosen_pair:
        return

    session_item, backups = chosen_pair
    confirm = inquirer.confirm(
        message=f"Restore original snapshot from {backups[0].name}? This will overwrite current session state.",
        default=False,
    ).execute()

    if confirm and session_item.rollout_path:
        try:
            restored = restore_session_backup(
                session_item.id, session_item.rollout_path, backups[0]
            )
            print_success(f"Successfully restored session from: {restored.name}")
        except (OSError, ValueError, RuntimeError) as exc:
            print_error(f"Restore failed: {exc!s}")
        inquirer.text(message="Press [Enter] to continue...").execute()


def _handle_version_update() -> None:
    """Check version status and offer self-update in the TUI."""
    from codex_mgr.core.version import check_for_updates, get_current_version, perform_self_update

    v = get_current_version()
    with console.status("[dim]Checking for upstream releases...", spinner="dots"):
        has_up, latest, msg = check_for_updates()

    if has_up:
        panel_msg = (
            f"Installed Version: [bold accent]v{v}[/]\n"
            f"Latest Release   : [bold success]v{latest}[/]\n\n"
            f"A newer version of codex-mgr is available!"
        )
        console.print(Panel(panel_msg, title="✨ Update Available", border_style="warning"))

        do_update = inquirer.confirm(
            message=f"Upgrade codex-mgr to v{latest} now via pip?", default=True
        ).execute()

        if do_update:
            with console.status("[accent]Upgrading codex-mgr via pip...", spinner="dots"):
                ok, update_msg = perform_self_update()
            if ok:
                print_success(update_msg)
            else:
                print_error(update_msg)
    else:
        panel_msg = f"Installed Version: [bold success]v{v}[/]\nStatus           : {msg}"
        console.print(Panel(panel_msg, title="Version Status", border_style="success"))

    inquirer.text(message="Press [Enter] to return to the menu...").execute()
