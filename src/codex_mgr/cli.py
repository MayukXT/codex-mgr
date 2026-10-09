"""Command-line interface (CLI) entry point and command dispatcher.

Supports both no-arguments interactive TUI mode for non-technical users
and direct, scriptable CLI subcommands for power users.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List, Optional

from codex_mgr import __version__
from codex_mgr.config.portable import apply_import_bundle, export_bundle
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.backup import restore_session_backup
from codex_mgr.core.bridge import bridge_session_rollout
from codex_mgr.core.doctor import run_diagnostics
from codex_mgr.core.profiles import switch_active_provider
from codex_mgr.core.runner import launch_codex
from codex_mgr.core.sessions import find_session_by_id_or_title, list_sessions
from codex_mgr.tui.dashboard import run_main_dashboard
from codex_mgr.tui.doctor_view import render_doctor_report
from codex_mgr.tui.provider_wizard import run_provider_wizard
from codex_mgr.tui.session_picker import interactive_resume_session
from codex_mgr.utils.display import console, print_error, print_success, print_warning


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="cmgr",
        description="Codex Manager (cmgr): Universal multi-provider bridge & session manager for Codex CLI."
    )
    parser.add_argument("-v", "--version", action="version", version=f"codex-mgr {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume a session (interactive or direct ID)")
    p_resume.add_argument("session", nargs="?", help="Session UUID or title keyword")
    p_resume.add_argument("--to", dest="target_provider", help="Target provider to bridge session into")
    p_resume.add_argument("--last", action="store_true", help="Resume the most recent session automatically")

    # switch
    p_switch = subparsers.add_parser("switch", help="Switch active provider in config.toml")
    p_switch.add_argument("provider", nargs="?", help="Provider identifier (e.g. openai, agentrouter)")

    # doctor
    subparsers.add_parser("doctor", help="Run system diagnostics and verify environment health")

    # export
    p_export = subparsers.add_parser("export", help="Export configuration bundle to JSON")
    p_export.add_argument("-o", "--output", default="cmgr-config.json", help="Output file path")
    p_export.add_argument("--sanitize-keys", action="store_true", help="Redact API keys for safe sharing")

    # import
    p_import = subparsers.add_parser("import", help="Import configuration bundle from JSON")
    p_import.add_argument("file", help="Path to JSON bundle file")
    p_import.add_argument("--strategy", choices=["merge", "overwrite", "rename"], default="merge")

    # restore
    p_restore = subparsers.add_parser("restore", help="Restore session rollout from backup snapshot")
    p_restore.add_argument("session_id", help="UUID of session to restore")

    # provider
    p_provider = subparsers.add_parser("provider", help="Manage configured providers")
    p_provider.add_argument("action", choices=["list", "add"], default="list", nargs="?")

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    """Main execution function."""
    if argv is None:
        argv = sys.argv[1:]

    # If no arguments provided, launch the interactive TUI dashboard!
    if not argv:
        try:
            run_main_dashboard()
            return 0
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelled by user.[/]")
            return 0

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "resume":
        settings = load_settings()
        target_p = args.target_provider or settings.active_provider_id

        if args.last:
            all_s = list_sessions(limit=1)
            if not all_s:
                print_error("No sessions found to resume.")
                return 1
            target_session = all_s[0]
        elif args.session:
            target_session = find_session_by_id_or_title(args.session)
            if not target_session:
                print_error(f"Could not find session matching: {args.session}")
                return 1
        else:
            interactive_resume_session(target_provider_id=target_p)
            return 0

        # Auto-bridge if needed
        if target_session.rollout_path and target_session.origin_provider != target_p:
            ok, count, msg = bridge_session_rollout(
                target_session.rollout_path,
                target_provider_id=target_p,
                create_backup_snapshot=True
            )
            if ok:
                print_success(msg)
            else:
                print_error(f"Bridge failed: {msg}")
                return 1

        return launch_codex(session_id=target_session.id)

    elif args.command == "switch":
        settings = load_settings()
        if not args.provider:
            print_warning(f"Active provider is: {settings.active_provider_id}")
            console.print(f"Available: {', '.join(settings.providers.keys())}")
            return 0

        pid = args.provider.lower()
        if pid not in settings.providers:
            print_error(f"Provider '{pid}' not found. Available: {', '.join(settings.providers.keys())}")
            return 1

        target = settings.providers[pid]
        settings.active_provider_id = pid
        save_settings(settings, create_backup=False)

        ok, msg = switch_active_provider(target)
        if ok:
            print_success(msg)
            return 0
        else:
            print_error(msg)
            return 1

    elif args.command == "doctor":
        render_doctor_report()
        return 0

    elif args.command == "export":
        out_path = Path(args.output)
        saved = export_bundle(out_path, sanitize_keys=args.sanitize_keys)
        print_success(f"Configuration exported to: {saved.resolve()}")
        return 0

    elif args.command == "import":
        in_path = Path(args.file)
        if not in_path.exists():
            print_error(f"Import file not found: {in_path}")
            return 1
        added, updated = apply_import_bundle(in_path, conflict_strategy=args.strategy)
        print_success(f"Imported successfully: {added} added, {updated} updated.")
        return 0

    elif args.command == "restore":
        session = find_session_by_id_or_title(args.session_id)
        if not session or not session.rollout_path:
            print_error(f"Session '{args.session_id}' not found or rollout missing.")
            return 1
        try:
            restored = restore_session_backup(session.id, session.rollout_path)
            print_success(f"Successfully restored session from: {restored.name}")
            return 0
        except Exception as e:
            print_error(f"Restore failed: {str(e)}")
            return 1

    elif args.command == "provider":
        if args.action == "add":
            run_provider_wizard()
            return 0
        else:
            settings = load_settings()
            dot = get_symbol("dot")
            console.print("\n[bold accent]Configured Providers:[/]")
            for pid, p in settings.providers.items():
                active = " (ACTIVE)" if pid == settings.active_provider_id else ""
                console.print(f"{dot} [bold]{p.name}[/] ({pid}){active} [dim]Model: {p.default_model}[/]")
            return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
