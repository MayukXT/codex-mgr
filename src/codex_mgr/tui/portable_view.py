"""Interactive screens for configuration export, import, and portability."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.panel import Panel

from codex_mgr.config.portable import apply_import_bundle, export_bundle, inspect_import_bundle
from codex_mgr.utils.display import console, print_error, print_success, print_warning


def run_portable_menu() -> None:
    """Show the configuration backup, export, and import interactive menu."""
    while True:
        choice = inquirer.select(
            message="📦 Configuration Portability & Backups:",
            choices=[
                Choice(value="export", name="📤 Export Configuration Bundle (Save all providers & models to JSON)"),
                Choice(value="import", name="📥 Import Configuration Bundle (Load settings from JSON file)"),
                Choice(value="back", name="↩️  Back to Main Menu"),
            ],
            default="back"
        ).execute()

        if choice == "back":
            break

        if choice == "export":
            _handle_export()
        elif choice == "import":
            _handle_import()


def _handle_export() -> None:
    """Prompt for export options and execute configuration bundle export."""
    default_name = f"cmgr-export-{datetime.now().strftime('%Y%m%d')}.json"
    raw_path = inquirer.text(
        message="Enter output file path:",
        default=default_name
    ).execute().strip() or default_name

    sanitize = inquirer.confirm(
        message="Sanitize / redact API keys for safe public or team sharing?",
        default=False
    ).execute()

    out_file = Path(raw_path)
    try:
        saved_path = export_bundle(out_file, sanitize_keys=sanitize)
        msg = (
            f"[bold success]Configuration exported successfully![/]\n\n"
            f"File: [bold accent]{saved_path.resolve()}[/]\n"
            f"Sanitized: {'Yes (API keys redacted)' if sanitize else 'No (Full keys included)'}"
        )
        console.print(Panel(msg, title="Export Complete", border_style="success"))
    except Exception as exc:
        print_error(f"Export failed: {str(exc)}")


def _handle_import() -> None:
    """Prompt for import bundle path, display diff, and apply changes."""
    raw_path = inquirer.text(message="Enter the path to the JSON export bundle:").execute().strip()
    bundle_path = Path(raw_path)

    if not bundle_path.exists():
        print_error(f"File not found: {bundle_path}")
        return

    try:
        new_providers, conflicts = inspect_import_bundle(bundle_path)

        console.print(Panel(
            f"[bold]Discovered in Bundle:[/]\n"
            f"• New Providers to Add   : {', '.join(new_providers) if new_providers else '[dim]none[/]'}\n"
            f"• Existing Providers (Diff): {', '.join(conflicts) if conflicts else '[dim]none[/]'}",
            title="Import Preview",
            border_style="accent"
        ))

        strategy = "merge"
        if conflicts:
            strategy = inquirer.select(
                message="How would you like to resolve existing provider conflicts?",
                choices=[
                    Choice(value="merge", name="Merge (Keep existing settings, merge newly found models)"),
                    Choice(value="overwrite", name="Overwrite (Replace existing settings with imported values)"),
                    Choice(value="rename", name="Keep Both (Import conflicting providers with '_imported' suffix)"),
                ],
                default="merge"
            ).execute()

        confirm = inquirer.confirm(
            message="Proceed with configuration import?",
            default=True
        ).execute()

        if not confirm:
            print_warning("Import cancelled.")
            return

        added, updated = apply_import_bundle(bundle_path, conflict_strategy=strategy)  # type: ignore
        print_success(f"Import complete: {added} provider(s) added, {updated} updated.")

    except Exception as exc:
        print_error(f"Import failed: {str(exc)}")
