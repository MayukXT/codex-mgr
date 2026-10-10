"""Review exact configuration revisions with explicit restore and repair choices."""

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from codex_mgr.core.revisions import (
    RepairRequiredError,
    Revision,
    apply_revision_restore,
    list_revisions,
    prepare_revision_restore,
)
from codex_mgr.utils.display import console, print_error, print_success, select_menu


def pick_revision(kind: str) -> Revision | None:
    items = list_revisions(kind)
    if not items:
        console.print(f"No checksummed {kind} revisions.")
        return None
    offset = 0
    while True:
        size = max(1, min(12, console.height - 10))
        offset = min(offset, max(0, ((len(items) - 1) // size) * size))
        choices = [
            Choice(
                item.name,
                f"{item.created_at[:16]} {item.integrity} {item.name[-13:-5]}"[
                    : max(8, console.width - 6)
                ],
            )
            for item in items[offset : offset + size]
        ]
        if offset + size < len(items):
            choices.append(Choice("next", "Next page"))
        if offset:
            choices.append(Choice("previous", "Previous page"))
        choices.append(Choice(None, "Back"))
        selected = select_menu(f"{kind.capitalize()} revisions", choices, allow_back=True)
        if selected is None:
            return None
        if selected == "next":
            offset += size
        elif selected == "previous":
            offset -= size
        else:
            return next((item for item in items if item.name == selected), None)


def manage_configuration_revisions() -> None:
    scope = select_menu(
        "Restore configuration",
        [
            Choice("config", "Codex configuration"),
            Choice("settings", "Manager settings"),
            Choice("both", "Both files"),
            Choice("repair", "Repair current files"),
            Choice(None, "Back"),
        ],
        allow_back=True,
    )
    if scope is None:
        return
    repair = scope == "repair"
    if repair:
        scope = "both"
        if not inquirer.confirm(
            message="Allow repair of unreadable current files from valid revisions?", default=False
        ).execute():
            return
    try:
        config = pick_revision("config") if scope in {"config", "both"} else None
        if scope in {"config", "both"} and config is None:
            return
        settings = pick_revision("settings") if scope in {"settings", "both"} else None
        if scope in {"settings", "both"} and settings is None:
            return
        try:
            plan = prepare_revision_restore(config=config, settings=settings, repair=repair)
        except RepairRequiredError:
            if not inquirer.confirm(
                message="Current files are malformed. Repair from these revisions?", default=False
            ).execute():
                return
            plan = prepare_revision_restore(config=config, settings=settings, repair=True)
        for name, revision in (("Config", config), ("Settings", settings)):
            if revision is not None:
                console.print(f"\n{name}: {revision.name}", markup=False)
        console.print(
            "\nCurrent files will be saved first. Codex must accept the restored selection.\n"
        )
        if config is not None and settings is not None:
            console.print("The config revision determines the selected connection and model.\n")
        if not inquirer.confirm(message="Restore these revisions?", default=False).execute():
            return
        ok, message = apply_revision_restore(plan)
        (print_success if ok else print_error)(message)
    except (OSError, ValueError, RuntimeError):
        print_error("Restore failed. Check revision integrity, current files and backup storage.")
