"""Paged recovery-copy selection with explicit, reviewed publication."""

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from codex_mgr.core.history_copy import list_history_copies, read_history_copy, recover_history_copy
from codex_mgr.utils.display import console, print_error, print_success, select_menu


def show_history_copy_details(info, *, target_verified=False) -> None:
    console.print()
    for label, value in (
        ("Copy", info.id),
        ("Original", info.original_id),
        ("Connection", info.provider),
        ("State", info.state),
        ("Sources", info.source_count),
        ("Encrypted items removed", info.sanitized_count),
        ("Bundle", "verified"),
        (
            "Target",
            (
                "present (frozen prefix verified)"
                if target_verified
                else "present (content unchecked)"
            )
            if info.target_present
            else "missing",
        ),
    ):
        console.print(f"{label}: {value}", markup=False)
    console.print()


def manage_history_copies() -> None:
    offset = 0
    while True:
        size = max(1, min(10, console.height - 10))
        try:
            page = list_history_copies(limit=size + 1, offset=offset)
        except (OSError, ValueError, RuntimeError):
            print_error("History copy inventory is unavailable. Check private backup storage.")
            return
        if not page and offset:
            offset = max(0, offset - size)
            continue
        if not page:
            console.print("No independent history copies.", markup=False)
            return
        choices = [
            Choice(item.id, f"{item.id[:8]}...{item.id[-8:]}  {item.status}")
            for item in page[:size]
        ]
        if offset:
            choices.append(Choice("previous", "Previous page"))
        if len(page) > size:
            choices.append(Choice("next", "Next page"))
        choices.append(Choice(None, "Back"))
        selected = select_menu("History copies", choices, allow_back=True)
        if selected is None:
            return
        if selected == "previous":
            offset = max(0, offset - size)
            continue
        if selected == "next":
            offset += size
            continue
        try:
            info = read_history_copy(selected)
            show_history_copy_details(info)
            action = select_menu(
                "Copy recovery",
                [
                    Choice("settle", "Verify and settle"),
                    Choice("publish", "Publish saved copy"),
                    Choice(None, "Back"),
                ],
                allow_back=True,
            )
            if action is None:
                continue
            publish = action == "publish"
            message = "Publish this saved copy?" if publish else "Settle matching publication?"
            if publish and info.sanitized_count:
                message = f"Publish this copy with {info.sanitized_count} encrypted items removed?"
            if not inquirer.confirm(message=message, default=False).execute():
                continue
            result = recover_history_copy(
                info.id,
                publish=publish,
                allow_reasoning_loss=publish,
                expected_digest=info.review_digest,
            )
            print_success(
                "Recovery settled. Matching later turns were preserved."
                if result.target_present
                else "Bundle verified. Missing session remains unpublished."
            )
        except (OSError, ValueError, RuntimeError):
            print_error(
                "Recovery stopped. Check bundle integrity, changed content, duplicate identities "
                "and active writers."
            )
