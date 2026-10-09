"""Paged session search, details and explicit conversion consent."""

from __future__ import annotations

from pathlib import Path

from InquirerPy import inquirer
from InquirerPy.base.control import Choice
from rich.text import Text

from codex_mgr.config.settings import load_settings
from codex_mgr.core.resume import preview_resume, resume_session
from codex_mgr.core.sessions import SessionItem, discover_sessions, filter_sessions
from codex_mgr.utils.display import console, print_error, print_warning, select_menu, single_line


def pick_session(
    *,
    workspace: str | Path | None = None,
    include_archived: bool = False,
    with_backups: bool = False,
) -> SessionItem | None:
    query = ""
    offset = 0
    catalogs = {}
    while True:
        if include_archived not in catalogs:
            catalogs[include_archived] = discover_sessions(include_archived=include_archived)
        catalog = catalogs[include_archived]
        items = filter_sessions(catalog.sessions, search=query, workspace=workspace)
        if with_backups:
            from codex_mgr.core.backup import list_session_backups

            items = [item for item in items if list_session_backups(item.id)]
        page_size = max(1, min(12, console.height - 12))
        offset = min(offset, max(0, ((len(items) - 1) // page_size) * page_size))
        choices = []
        for item in items[offset : offset + page_size]:
            width = max(10, console.width - 8)
            label = single_line(item.thread_name)[:width]
            choices.append(Choice(item, label))
        if offset:
            choices.append(Choice("previous", "Previous page"))
        if offset + page_size < len(items):
            choices.append(Choice("next", "Next page"))
        choices.extend(
            [
                Choice("search", "Search"),
                Choice("refresh", "Refresh"),
                Choice("workspace", "All workspaces" if workspace else "Current workspace"),
                Choice("archived", "Hide archived" if include_archived else "Include archived"),
                Choice(None, "Back"),
            ]
        )
        if catalog.skipped_rollouts or catalog.skipped_index_records or catalog.unreadable_sources:
            print_warning(
                "Some session sources are unreadable or invalid. Use Codex's history picker for other storage formats."
            )
        start = offset + 1 if items else 0
        selected = select_menu(
            f"Sessions {start}-{min(offset + page_size, len(items))} of {len(items)}:", choices
        )
        if selected is None:
            return None
        if isinstance(selected, SessionItem):
            return selected
        if selected == "next":
            offset += page_size
        elif selected == "previous":
            offset -= page_size
        elif selected == "search":
            query = (
                inquirer.text(message="Title or UUID:", default=query, mandatory=False).execute()
                or ""
            )
            offset = 0
        elif selected == "workspace":
            workspace = None if workspace else Path.cwd()
            offset = 0
        elif selected == "archived":
            include_archived = not include_archived
            offset = 0
        elif selected == "refresh":
            catalogs.clear()


def show_session_details(session: SessionItem) -> None:
    console.print()
    console.print(Text(single_line(session.thread_name), style="bold"))
    for key, value in [
        ("UUID", session.id),
        ("Updated", session.relative_time),
        ("Connection", session.origin_provider),
        ("Workspace", session.cwd or "Unknown"),
    ]:
        console.print(Text(f"{key}: {single_line(value)}"))
    if session.archived:
        console.print("Archived", markup=False)
    console.print()


def pick_snapshot(paths: list[Path]) -> Path | None:
    offset = 0
    while True:
        size = max(1, min(10, console.height - 10))
        choices = [
            Choice(
                path,
                f"{offset + index + 1}. {single_line(path.name)[: max(10, console.width - 12)]}",
            )
            for index, path in enumerate(paths[offset : offset + size])
        ]
        if offset:
            choices.append(Choice("previous", "Previous page"))
        if offset + size < len(paths):
            choices.append(Choice("next", "Next page"))
        choices.append(Choice(None, "Back"))
        selected = select_menu(
            f"Snapshots {offset + 1 if paths else 0}-{min(offset + size, len(paths))} of {len(paths)}:",
            choices,
        )
        if selected is None or isinstance(selected, Path):
            return selected
        if selected == "next":
            offset += size
        elif selected == "previous":
            offset = max(0, offset - size)


def interactive_resume_session(
    target_provider_id: str | None = None,
    *,
    workspace: str | Path | None = None,
    include_archived: bool = False,
) -> None:
    session = pick_session(workspace=workspace, include_archived=include_archived)
    if session is None:
        return
    show_session_details(session)
    settings = load_settings()
    try:
        preview = preview_resume(session, target_provider_id, settings=settings)
    except (OSError, ValueError, RuntimeError):
        print_error(
            "Cannot prepare this session. Check its identity, selected connection and credentials; encrypted or paginated history may require the original Codex connection."
        )
        return
    if preview.changed_records:
        console.print(f"Target connection: {preview.connection_id}", markup=False)
        if preview.encrypted_items:
            print_warning(
                f"Conversion removes {preview.encrypted_items} encrypted items. Readable summaries remain; the original is saved in a recovery snapshot."
            )
        if not inquirer.confirm(
            message="Save a snapshot, convert and resume?", default=False
        ).execute():
            return
    elif select_menu("Session:", [Choice("resume", "Resume"), Choice(None, "Back")]) != "resume":
        return
    code, message = resume_session(
        session,
        target_provider_id,
        allow_reasoning_loss=True,
        expected_digest=preview.session_digest,
        settings=settings,
    )
    if code:
        print_error(message)
