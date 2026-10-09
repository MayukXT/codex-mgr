"""Export/import preview with sanitized defaults and explicit conflict choices."""

from datetime import datetime, timezone
from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from codex_mgr.config.portable import apply_import_bundle, export_bundle, inspect_import_bundle
from codex_mgr.utils.display import console, print_error, print_success, select_menu


def run_portable_menu():
    while True:
        choice = select_menu(
            "Configuration:",
            [Choice("export", "Export"), Choice("import", "Import"), Choice(None, "Back")],
        )
        if not choice:
            return
        try:
            if choice == "export":
                default = datetime.now(timezone.utc).strftime("cmgr-export-%Y%m%d-%H%M%S.json")
                value = inquirer.text(message="Output file:", default=default).execute().strip()
                if not value:
                    continue
                include = inquirer.confirm(
                    message="Include plaintext credentials in this private file?", default=False
                ).execute()
                saved = export_bundle(Path(value), sanitize_keys=not include)
                print_success(f"Exported: {saved.name}")
                console.print(
                    "Review endpoint names, model names and environment references before sharing.",
                    markup=False,
                )
            else:
                value = inquirer.text(message="Import file:").execute().strip()
                if not value:
                    continue
                path = Path(value)
                added, conflicts = inspect_import_bundle(path)
                console.print(
                    f"New: {', '.join(added) or 'None'}\nExisting: {', '.join(conflicts) or 'None'}",
                    markup=False,
                )
                strategy = (
                    select_menu(
                        "Existing connections:",
                        [
                            Choice("merge", "Keep settings; add models"),
                            Choice("rename", "Keep both"),
                            Choice("overwrite", "Replace settings"),
                        ],
                        default="merge",
                        allow_back=True,
                    )
                    if conflicts
                    else "merge"
                )
                if (
                    not strategy
                    or not inquirer.confirm(message="Apply import?", default=False).execute()
                ):
                    continue
                added, updated = apply_import_bundle(path, strategy)
                print_success(f"Imported: {added} added, {updated} updated.")
        except (OSError, ValueError, RuntimeError):
            print_error(
                "Operation failed. Check bundle fields, existing files, credentials and permissions."
            )
