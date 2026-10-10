"""Main interactive TUI dashboard for codex-mgr.

Presents an elegant, arrow-key navigable dashboard that coordinates
session resumption, provider switching, models, backups, and health checks.
"""

from __future__ import annotations

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.text import Text

from codex_mgr.config.settings import load_settings
from codex_mgr.core.activation import activate_provider
from codex_mgr.core.backup import list_session_backups, restore_session_backup
from codex_mgr.core.resume import start_session
from codex_mgr.tui.doctor_view import render_doctor_report
from codex_mgr.tui.model_picker import manage_provider_models
from codex_mgr.tui.portable_view import run_portable_menu
from codex_mgr.tui.provider_wizard import run_provider_wizard
from codex_mgr.tui.session_picker import (
    interactive_resume_session,
    pick_session,
    pick_snapshot,
    show_session_details,
)
from codex_mgr.utils.display import (
    console,
    print_banner,
    print_error,
    print_success,
    print_warning,
    select_menu,
)


def run_main_dashboard() -> None:
    """Run the primary interactive loop."""
    while True:
        try:
            settings = load_settings()
        except (OSError, ValueError, RuntimeError):
            print_error("Current settings could not be read. Choose a recovery action.")
            action = select_menu(
                "Recovery",
                [
                    Choice("revisions", "Configuration revisions"),
                    Choice("recovery", "Activation recovery"),
                    Choice(None, "Exit"),
                ],
                allow_back=True,
            )
            if action == "revisions":
                from codex_mgr.tui.revision_view import manage_configuration_revisions

                manage_configuration_revisions()
            elif action == "recovery":
                from codex_mgr.tui.recovery_view import manage_activation_recovery

                manage_activation_recovery()
            else:
                return
            continue
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
            code, message = start_session(settings=settings)
            (print_success if code == 0 else print_error)(message)

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
            message="Tools",
            choices=[
                Choice(value="doctor", name="1. Diagnostics"),
                Choice(value="revert", name="2. Restore session"),
                Choice(value="port", name="3. Export and import"),
                Choice(value="update", name="4. Updates"),
                Choice(value="recovery", name="5. Activation recovery"),
                Choice(value="revisions", name="6. Configuration revisions"),
                Choice(value=None, name="Back"),
            ],
            default="doctor",
            allow_back=True,
            shortcuts={
                "1": "doctor",
                "2": "revert",
                "3": "port",
                "4": "update",
                "5": "recovery",
                "6": "revisions",
            },
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
        elif action == "recovery":
            from codex_mgr.tui.recovery_view import manage_activation_recovery

            manage_activation_recovery()
        elif action == "revisions":
            from codex_mgr.tui.revision_view import manage_configuration_revisions

            manage_configuration_revisions()


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
    session_item = pick_session(include_archived=True, with_backups=True)
    if session_item is None:
        return
    show_session_details(session_item)
    try:
        session_item.require_rollout()
        snapshot = pick_snapshot(list_session_backups(session_item.id))
    except (OSError, ValueError):
        print_error("Cannot restore this session. Check its identity and duplicate rollout files.")
        return
    if snapshot is None:
        return
    console.print(f"Snapshot: {snapshot.name}", markup=False)
    confirm = inquirer.confirm(
        message="Restore this snapshot? Current content will be saved first.",
        default=False,
    ).execute()

    if confirm and session_item.rollout_path:
        try:
            restored = restore_session_backup(session_item.id, session_item.rollout_path, snapshot)
            print_success(f"Successfully restored session from: {restored.name}")
        except (OSError, ValueError, RuntimeError):
            print_error(
                "Restore failed. Check snapshot integrity, session writers and backup storage."
            )
        inquirer.text(message="Press [Enter] to continue...").execute()


def _handle_version_update() -> None:
    """Check version status and offer self-update in the TUI."""
    from codex_mgr.core.version import check_for_updates, get_current_version, perform_self_update

    with console.status("[dim]Checking for upstream releases...", spinner="dots"):
        has_up, latest, msg = check_for_updates()

    console.print(Text(f"Installed: v{get_current_version()}", style="bold accent"))
    if latest is None:
        print_error(msg)
    elif has_up:
        print_warning(msg)

        do_update = inquirer.confirm(
            message=f"Install cmgr-cli v{latest} with this Python's pip?", default=False
        ).execute()

        if do_update:
            with console.status("[accent]Upgrading codex-mgr via pip...", spinner="dots"):
                ok, update_msg = perform_self_update(latest)
            if ok:
                print_success(update_msg)
            else:
                print_error(update_msg)
    else:
        print_success(msg)

    inquirer.text(message="Press [Enter] to return to the menu...").execute()
