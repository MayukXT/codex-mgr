"""Explicit choices for recovering changes or keeping reviewed current files."""

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from codex_mgr.core.activation import (
    accept_external_activation,
    list_pending_activations,
    recover_pending_activation,
)
from codex_mgr.utils.display import console, print_error, print_success, select_menu


def manage_activation_recovery() -> None:
    while True:
        try:
            pending = list_pending_activations()
        except (OSError, ValueError, RuntimeError):
            print_error("Recovery records are unreadable or invalid. Check backup storage.")
            return
        if not pending:
            console.print("No pending activation transactions.")
            return
        selected = select_menu(
            "Activation recovery",
            [
                Choice("auto", "Recover matching files"),
                *[
                    Choice(
                        item.id,
                        f"{item.id[:12]}  config: {item.config_status}; settings: {item.settings_status}",
                    )
                    for item in pending
                ],
                Choice(None, "Back"),
            ],
            allow_back=True,
        )
        if selected is None:
            return
        if selected == "auto":
            ok, message = recover_pending_activation()
            (print_success if ok else print_error)(message)
            continue
        item = next((item for item in pending if item.id == selected), None)
        if item is None:
            return
        console.print(
            f"\nTransaction: {item.id}\nConfig: {item.config_status}\nSettings: {item.settings_status}\n",
            markup=False,
        )
        console.print(
            "Keeping current files saves snapshots and clears this pending transaction.\nIt does not validate their settings.\n"
        )
        if inquirer.confirm(message="Keep these current files?", default=False).execute():
            ok, message = accept_external_activation(item.id, expected=item.expected)
            (print_success if ok else print_error)(message)
