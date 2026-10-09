"""Main interactive TUI dashboard for codex-mgr.

Presents an elegant, arrow-key navigable dashboard that coordinates
session resumption, provider switching, models, backups, and health checks.
"""

from __future__ import annotations

import sys
from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.panel import Panel

from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.backup import list_session_backups, restore_session_backup
from codex_mgr.core.profiles import switch_active_provider
from codex_mgr.core.runner import launch_codex
from codex_mgr.core.sessions import list_sessions
from codex_mgr.tui.doctor_view import render_doctor_report
from codex_mgr.tui.model_picker import manage_provider_models
from codex_mgr.tui.portable_view import run_portable_menu
from codex_mgr.tui.provider_wizard import run_provider_wizard
from codex_mgr.tui.session_picker import interactive_resume_session
from codex_mgr.utils.display import console, get_symbol, print_banner, print_error, print_success, print_warning


def run_main_dashboard() -> None:
    """Run the primary interactive loop."""
    while True:
        settings = load_settings()
        active_p = settings.providers.get(settings.active_provider_id)
        active_name = active_p.name if active_p else settings.active_provider_id
        active_model = active_p.default_model if active_p else "default"

        console.clear()
        print_banner(active_provider=active_name, active_model=active_model)

        action = inquirer.select(
            message="What would you like to do? (Use arrow keys ⬆/⬇, press Enter):",
            choices=[
                Choice(value="resume", name="🚀  Resume a Session        (Auto-bridges reasoning blobs seamlessly)"),
                Choice(value="switch", name="🔄  Switch Active Provider  (Switch provider & model in config.toml)"),
                Choice(value="manage", name="➕  Manage Providers        (Add, edit, or configure models)"),
                Choice(value="new",    name="⚡  Start New Session       (Launch fresh session with active provider)"),
                Choice(value="doctor", name="🩺  Doctor & Health Check   (Verify configs, paths, and connectivity)"),
                Choice(value="update", name="✨  Version & Updates       (Check releases and upgrade codex-mgr)"),
                Choice(value="port",   name="📦  Export & Import Setup   (Backup, share, or migrate configurations)"),
                Choice(value="revert", name="↩️   Undo / Revert Session   (Restore original session from backup)"),
                Choice(value="exit",   name="🚪  Exit"),
            ],
            default="resume",
            pointer="❯",
        ).execute()

        if action == "exit":
            console.print("[dim]Goodbye![/]")
            break

        elif action == "resume":
            interactive_resume_session()

        elif action == "switch":
            _handle_switch_provider()

        elif action == "manage":
            _handle_manage_providers()

        elif action == "new":
            console.print(f"\n[bold accent]{get_symbol('rocket')} Launching fresh Codex session with {active_name}...[/]\n")
            launch_codex()

        elif action == "doctor":
            render_doctor_report()
            inquirer.text(message="Press [Enter] to return to the menu...").execute()

        elif action == "update":
            _handle_version_update()

        elif action == "port":
            run_portable_menu()

        elif action == "revert":
            _handle_revert_session()


def _handle_switch_provider() -> None:
    """Prompt to switch active provider."""
    settings = load_settings()
    choices = []

    for pid, p in settings.providers.items():
        is_active = pid == settings.active_provider_id
        mark = "✓ " if is_active else "  "
        label = f"{mark}{p.name:<25} [dim]Model: {p.default_model}[/]"
        choices.append(Choice(value=pid, name=label))

    choices.append(Choice(value=None, name="↩️  Back"))

    chosen_pid = inquirer.select(
        message="Select target provider to activate:",
        choices=choices,
        default=settings.active_provider_id
    ).execute()

    if chosen_pid and chosen_pid != settings.active_provider_id:
        target = settings.providers[chosen_pid]
        settings.active_provider_id = chosen_pid
        save_settings(settings, create_backup=False)

        ok, msg = switch_active_provider(target)
        if ok:
            print_success(msg)
        else:
            print_error(msg)
        inquirer.text(message="Press [Enter] to continue...").execute()


def _handle_manage_providers() -> None:
    """Submenu for adding providers or managing models."""
    settings = load_settings()
    choices = [
        Choice(value="__new__", name="➕  Add New Provider (Guided 6-step wizard)"),
    ]
    for pid, p in settings.providers.items():
        choices.append(Choice(value=pid, name=f"⚙️   Manage Models for: {p.name}"))
    choices.append(Choice(value=None, name="↩️  Back"))

    chosen = inquirer.select(
        message="Select provider to manage or configure:",
        choices=choices
    ).execute()

    if chosen == "__new__":
        run_provider_wizard()
    elif chosen:
        manage_provider_models(chosen)


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
    choices.append(Choice(value=None, name="Cancel"))

    chosen_pair = inquirer.select(
        message="Select session to restore from backup:",
        choices=choices
    ).execute()

    if not chosen_pair:
        return

    session_item, backups = chosen_pair
    confirm = inquirer.confirm(
        message=f"Restore original snapshot from {backups[0].name}? This will overwrite current session state.",
        default=False
    ).execute()

    if confirm and session_item.rollout_path:
        try:
            restored = restore_session_backup(session_item.id, session_item.rollout_path, backups[0])
            print_success(f"Successfully restored session from: {restored.name}")
        except Exception as exc:
            print_error(f"Restore failed: {str(exc)}")
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
            message=f"Upgrade codex-mgr to v{latest} now via pip?",
            default=True
        ).execute()

        if do_update:
            with console.status("[accent]Upgrading codex-mgr via pip...", spinner="dots"):
                ok, update_msg = perform_self_update()
            if ok:
                print_success(update_msg)
            else:
                print_error(update_msg)
    else:
        panel_msg = (
            f"Installed Version: [bold success]v{v}[/]\n"
            f"Status           : {msg}"
        )
        console.print(Panel(panel_msg, title="Version Status", border_style="success"))

    inquirer.text(message="Press [Enter] to return to the menu...").execute()
