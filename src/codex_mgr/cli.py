"""Command-line interface (CLI) entry point and command dispatcher.

Supports both no-arguments interactive TUI mode for non-technical users
and direct, scriptable CLI subcommands for power users.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from codex_mgr import __version__
from codex_mgr.config.portable import apply_import_bundle, export_bundle
from codex_mgr.config.schema import ModelInfo
from codex_mgr.config.settings import load_settings
from codex_mgr.core.activation import activate_provider, recover_pending_activation
from codex_mgr.core.backup import list_session_backups, restore_session_backup
from codex_mgr.core.models import refresh_provider_models, select_model, test_provider_connectivity
from codex_mgr.core.providers import delete_provider
from codex_mgr.core.resume import resume_session
from codex_mgr.core.sessions import (
    AmbiguousSessionError,
    find_session_by_id_or_title,
    list_sessions,
)
from codex_mgr.tui.dashboard import run_main_dashboard
from codex_mgr.tui.doctor_view import render_doctor_report
from codex_mgr.tui.provider_wizard import run_provider_wizard
from codex_mgr.tui.session_picker import interactive_resume_session
from codex_mgr.utils.display import (
    console,
    get_symbol,
    print_error,
    print_success,
    print_warning,
    single_line,
)


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="cmgr",
        description="Providers, models and session recovery for Codex CLI.",
    )
    parser.add_argument("-v", "--version", action="version", version=f"codex-mgr {__version__}")

    subparsers = parser.add_subparsers(dest="command", help="Available subcommands")

    # resume
    p_resume = subparsers.add_parser("resume", help="Resume a session (interactive or direct ID)")
    p_resume.add_argument("session", nargs="?", help="Session UUID or title keyword")
    p_resume.add_argument(
        "--to", dest="target_provider", help="Target provider to bridge session into"
    )
    p_resume.add_argument(
        "--last", action="store_true", help="Resume the most recent session automatically"
    )
    p_resume.add_argument("--workspace", help="Filter by the session's working directory")
    p_resume.add_argument("--include-archived", action="store_true")
    p_resume.add_argument(
        "--allow-reasoning-loss",
        action="store_true",
        help="Allow backed-up conversion to remove encrypted reasoning",
    )

    p_sessions = subparsers.add_parser(
        "sessions", help="Find saved sessions by UUID, title or workspace"
    )
    p_sessions.add_argument("--search", help="UUID or title substring")
    p_sessions.add_argument("--workspace", help="Exact working directory")
    p_sessions.add_argument("--include-archived", action="store_true")
    p_sessions.add_argument("--limit", type=int, help="Page size (default: all sessions)")
    p_sessions.add_argument("--offset", type=int, default=0)
    p_sessions.add_argument("--json", action="store_true", help="Machine-readable session metadata")

    # switch
    p_switch = subparsers.add_parser("switch", help="Switch active provider in config.toml")
    p_switch.add_argument(
        "provider", nargs="?", help="Provider identifier (e.g. openai, agentrouter)"
    )

    # doctor
    doctor_parser = subparsers.add_parser("doctor", help="Check local state without changing it")
    doctor_parser.add_argument(
        "--network", action="store_true", help="Also request the selected provider's model catalog"
    )
    subparsers.add_parser("recover", help="Recover an interrupted configuration activation")

    # version & update
    subparsers.add_parser("version", help="Show installed version and check for updates")
    p_update = subparsers.add_parser("update", help="Check and self-update codex-mgr via pip")
    p_update.add_argument(
        "--check", action="store_true", help="Only check for updates without installing"
    )

    # export
    p_export = subparsers.add_parser("export", help="Export configuration bundle to JSON")
    p_export.add_argument("-o", "--output", default="cmgr-config.json", help="Output file path")
    p_export.add_argument(
        "--sanitize-keys", action="store_true", help="Redact API keys for safe sharing"
    )
    p_export.add_argument(
        "--include-secrets",
        action="store_true",
        help="Include plaintext credentials in this private export",
    )
    p_export.add_argument("--overwrite", action="store_true", help="Replace the chosen export file")

    # import
    p_import = subparsers.add_parser("import", help="Import configuration bundle from JSON")
    p_import.add_argument("file", help="Path to JSON bundle file")
    p_import.add_argument("--strategy", choices=["merge", "overwrite", "rename"], default="merge")

    # restore
    p_restore = subparsers.add_parser(
        "restore", help="Restore session rollout from backup snapshot"
    )
    p_restore.add_argument("session_id", help="UUID of session to restore")
    p_restore.add_argument("--list", action="store_true", help="List available recovery snapshots")
    p_restore.add_argument("--snapshot", help="Exact snapshot filename from --list")

    # provider
    p_provider = subparsers.add_parser("provider", help="Manage configured providers")
    p_provider.add_argument(
        "action", choices=["list", "add", "edit", "test", "delete"], default="list", nargs="?"
    )
    p_provider.add_argument("provider_id", nargs="?")

    p_model = subparsers.add_parser("model", help="List, select and manage model IDs")
    p_model.add_argument(
        "action", choices=["list", "use", "add", "remove", "catalog"], default="list", nargs="?"
    )
    p_model.add_argument("model_id", nargs="?")
    p_model.add_argument("--provider", dest="provider_id")
    p_model.add_argument("--refresh", action="store_true", help="Read the provider catalog")
    p_model.add_argument("--name", help="Manual model display name")
    p_model.add_argument("--context", type=int, help="Documented context capacity")
    p_model.add_argument("--output", help="New Codex catalog JSON file")
    p_model.add_argument(
        "--instructions-file", help="Explicit Codex instruction template for the custom catalog"
    )

    return parser


def _main(argv: list[str] | None = None) -> int:
    """Main execution function."""
    if argv is None:
        argv = sys.argv[1:]

    # If no arguments provided, launch the interactive TUI dashboard!
    if not argv:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print_error("The menu needs an interactive terminal. Use cmgr --help for commands.")
            return 1
        try:
            run_main_dashboard()
            return 0
        except KeyboardInterrupt:
            console.print("\n[dim]Cancelled by user.[/]")
            return 0

    parser = build_parser()
    args = parser.parse_args(argv)
    if (
        (args.command == "provider" and args.action in {"add", "edit"})
        or (args.command == "resume" and not args.session and not args.last)
    ) and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        print_error(
            "This action needs an interactive terminal. Use a direct command or an import bundle."
        )
        return 1

    if args.command == "sessions":
        import json

        items = list_sessions(
            args.limit,
            offset=args.offset,
            search=args.search,
            workspace=args.workspace,
            include_archived=args.include_archived,
        )
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "id": item.id,
                            "title": item.thread_name,
                            "updated_at": item.updated_at,
                            "cwd": item.cwd,
                            "archived": item.archived,
                            "available": item.rollout_path is not None,
                            "ambiguous": bool(item.duplicate_paths),
                        }
                        for item in items
                    ],
                    ensure_ascii=True,
                ),
            )
        else:
            for item in items:
                state = (
                    "duplicate files"
                    if item.duplicate_paths
                    else "missing file"
                    if item.rollout_path is None
                    else "archived"
                    if item.archived
                    else item.relative_time
                )
                console.print(
                    f"{item.id}  {single_line(item.thread_name)}  ({state})", markup=False
                )
        return 0

    if args.command == "resume":
        settings = load_settings()
        target_p = args.target_provider or settings.active_provider_id

        if args.last:
            all_s = list_sessions(
                limit=1, workspace=args.workspace, include_archived=args.include_archived
            )
            if not all_s:
                print_error("No sessions found to resume.")
                return 1
            target_session = all_s[0]
        elif args.session:
            target_session = find_session_by_id_or_title(
                args.session, workspace=args.workspace, include_archived=args.include_archived
            )
            if not target_session:
                print_error(f"Could not find session matching: {args.session}")
                return 1
        else:
            interactive_resume_session(
                target_provider_id=target_p,
                workspace=args.workspace,
                include_archived=args.include_archived,
            )
            return 0

        code, message = resume_session(
            target_session,
            target_p,
            allow_reasoning_loss=args.allow_reasoning_loss,
            settings=settings,
        )
        if code:
            print_error(message)
        return code

    elif args.command == "switch":
        settings = load_settings()
        if not args.provider:
            print_warning(f"Active provider is: {settings.active_provider_id}")
            console.print(f"Available: {', '.join(settings.providers.keys())}")
            return 0

        pid = args.provider.lower()
        if pid not in settings.providers:
            print_error(
                f"Provider '{pid}' not found. Available: {', '.join(settings.providers.keys())}"
            )
            return 1

        ok, msg = activate_provider(pid)
        if ok:
            print_success(msg)
            return 0
        else:
            print_error(msg)
            return 1

    elif args.command == "recover":
        ok, msg = recover_pending_activation()
        (print_success if ok else print_error)(msg)
        return 0 if ok else 1

    elif args.command == "doctor":
        return 0 if render_doctor_report(network=args.network) else 1

    elif args.command == "version":
        from codex_mgr.core.version import check_for_updates, get_current_version

        v = get_current_version()
        console.print(f"[bold accent]codex-mgr[/] version [bold success]v{v}[/]")
        with console.status("[dim]Checking for upstream updates...", spinner="dots"):
            has_up, latest, msg = check_for_updates()
        if latest is None:
            print_error(msg)
            return 1
        if has_up:
            print_warning(msg)
            console.print("Run [bold accent]cmgr update[/] to upgrade to the latest release.")
        else:
            print_success(msg)
        return 0

    elif args.command == "update":
        from codex_mgr.core.version import check_for_updates, perform_self_update

        with console.status("[dim]Checking for updates on PyPI...", spinner="dots"):
            has_up, latest, msg = check_for_updates()

        if latest is None:
            print_error(msg)
            return 1

        if not has_up:
            print_success(msg)
            return 0

        print_warning(msg)
        if args.check:
            return 0

        with console.status("[accent]Upgrading codex-mgr via pip...", spinner="dots"):
            ok, update_msg = perform_self_update(latest)
        if ok:
            print_success(update_msg)
            return 0
        else:
            print_error(update_msg)
            return 1

    elif args.command == "export":
        out_path = Path(args.output)
        if args.include_secrets and args.sanitize_keys:
            print_error("Choose either a sanitized export or --include-secrets.")
            return 1
        saved = export_bundle(
            out_path, sanitize_keys=not args.include_secrets, overwrite=args.overwrite
        )
        print_success(f"Configuration exported: {saved.name}")
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
        session = find_session_by_id_or_title(args.session_id, include_archived=True)
        if not session or not session.rollout_path:
            print_error(f"Session '{args.session_id}' not found or rollout missing.")
            return 1
        try:
            backups = list_session_backups(session.id)
            if args.list:
                for snapshot in backups:
                    legacy = " (legacy)" if snapshot.parent.name != session.id else ""
                    console.print(snapshot.name + legacy, markup=False)
                return 0
            selected = None
            if args.snapshot:
                matches = [snapshot for snapshot in backups if snapshot.name == args.snapshot]
                if len(matches) != 1:
                    print_error("Choose one unambiguous snapshot filename from --list.")
                    return 1
                selected = matches[0]
            session.require_rollout()
            restored = restore_session_backup(session.id, session.rollout_path, selected)
            print_success(f"Successfully restored session from: {restored.name}")
            return 0
        except (OSError, ValueError, RuntimeError):
            print_error(
                "Restore failed. Check snapshot integrity, session identity, active writers and backup storage."
            )
            return 1

    elif args.command == "provider":
        if args.action == "add":
            run_provider_wizard()
            return 0
        elif args.action in {"edit", "test", "delete"}:
            settings = load_settings()
            provider = settings.providers.get(args.provider_id)
            if provider is None:
                print_error("Choose a configured connection ID.")
                return 1
            if args.action == "edit":
                return 0 if run_provider_wizard(args.provider_id) else 1
            if args.action == "test":
                ok, message, elapsed = test_provider_connectivity(provider)
                (print_success if ok else print_error)(f"{message} ({elapsed} ms)")
            else:
                ok, message = delete_provider(settings, provider.id)
                (print_success if ok else print_error)(message)
            return 0 if ok else 1
        else:
            settings = load_settings()
            dot = get_symbol("dot")
            console.print("\n[bold accent]Configured Providers:[/]")
            for pid, p in settings.providers.items():
                active = " (ACTIVE)" if pid == settings.active_provider_id else ""
                console.print(
                    f"{dot} {p.name} ({pid}){active}  Model: {p.default_model if p.model_selected else 'Codex default'}",
                    markup=False,
                )
            return 0

    elif args.command == "model":
        from codex_mgr.core.activation import apply_settings

        settings = load_settings()
        provider = settings.providers.get(args.provider_id or settings.active_provider_id)
        if provider is None:
            print_error("Connection not found.")
            return 1
        if args.refresh:
            ok, _, error = refresh_provider_models(provider)
            if not ok:
                print_error(error or "Catalog unavailable.")
                return 1
        if args.action == "catalog":
            from codex_mgr.core.catalog import export_codex_catalog

            if not args.output or not args.instructions_file:
                print_error(
                    "Specify --output and --instructions-file; the catalog also controls Codex instructions."
                )
                return 1
            path = export_codex_catalog(
                provider,
                Path(args.output),
                instructions=Path(args.instructions_file).read_text(encoding="utf-8"),
            )
            print_success(f"Catalog saved: {path.name}")
            console.print(
                "Set model_catalog_json to this path in Codex config.toml. Catalog labels do not prove inference support.",
                markup=False,
            )
            return 0
        if args.action == "list":
            if args.refresh:
                ok, message = apply_settings(settings)
                if not ok:
                    print_error(message)
                    return 1
            for model in provider.models:
                console.print(f"{model.id}  {model.name}", markup=False)
            if not provider.models:
                print_warning("No catalog saved. Use --refresh or add an exact model ID.")
            return 0
        if not args.model_id:
            print_error("An exact model ID is required.")
            return 1
        if args.action == "use":
            select_model(provider, args.model_id)
        elif args.action == "add":
            if any(m.id == args.model_id for m in provider.models):
                print_error("That model is already registered.")
                return 1
            provider.models.append(
                ModelInfo(
                    id=args.model_id,
                    name=args.name or args.model_id,
                    context_window=args.context,
                    is_custom=True,
                )
            )
        else:
            if args.model_id == provider.default_model:
                print_error("Choose another default before removing this model.")
                return 1
            if not any(m.id == args.model_id for m in provider.models):
                print_error("Model not found.")
                return 1
            provider.models = [m for m in provider.models if m.id != args.model_id]
        ok, message = apply_settings(
            settings,
            activate_provider_id=provider.id
            if args.action == "use" and provider.id == settings.active_provider_id
            else None,
        )
        (print_success if ok else print_error)(message)
        return 0 if ok else 1

    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        return 130
    except AmbiguousSessionError as exc:
        print_error(str(exc))
        return 1
    except (OSError, ValueError, RuntimeError):
        print_error(
            "Operation failed. Check configuration, settings, permissions, or another writer."
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
