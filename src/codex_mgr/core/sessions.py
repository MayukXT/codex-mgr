"""Offline session discovery with explicit identity and ambiguity checks."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from codex_mgr.core.rollouts import (
    canonical_session_id,
    read_session_metadata,
    thread_id_from_filename,
)
from codex_mgr.core.rpc import CodexReader, CodexRpcError
from codex_mgr.utils.paths import get_codex_home, get_session_index_path, get_sessions_dir


class AmbiguousSessionError(ValueError):
    pass


@dataclass
class SessionItem:
    id: str
    thread_name: str
    updated_at: str
    relative_time: str
    rollout_path: Path | None
    origin_provider: str
    cwd: str | None = None
    archived: bool = False
    duplicate_paths: tuple[Path, ...] = ()

    def require_rollout(self) -> Path:
        if self.rollout_path is not None and self.rollout_path.exists():
            metadata = read_session_metadata(self.rollout_path)
            if metadata.canonical_history:
                from codex_mgr.core.history_lineage import LineageLimits, index_history_rollouts

                index = index_history_rollouts(get_codex_home(), LineageLimits().directory_entries)
                current = tuple(
                    path
                    for paths in index.values()
                    for path in paths
                    if thread_id_from_filename(path) == self.id
                )
                if len(current) > 1 or self.duplicate_paths:
                    return _native_selected_rollout(self.id, current)
                if len(current) == 1:
                    return current[0]
                raise FileNotFoundError("The canonical session has no current rollout file.")
        if self.duplicate_paths:
            return _native_selected_rollout(self.id, self.duplicate_paths)
        if self.rollout_path is None:
            raise FileNotFoundError("The indexed session has no readable rollout file.")
        return self.rollout_path


def _native_selected_rollout(session_id: str, candidates: tuple[Path, ...]) -> Path:
    """Resolve canonical replacements through native state, never timestamps."""
    try:
        if not all(read_session_metadata(path).canonical_history for path in candidates):
            raise ValueError
        with CodexReader() as reader:
            result = reader.request("thread/read", {"threadId": session_id, "includeTurns": False})
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != session_id:
            raise ValueError
        raw_path = thread.get("path")
        if not isinstance(raw_path, str) or not raw_path or not Path(raw_path).is_absolute():
            raise ValueError
        selected = Path(raw_path).resolve()
        if not selected.exists() and selected.name.endswith(".jsonl"):
            selected = selected.with_suffix(".jsonl.zst").resolve()
        if selected not in {path.resolve() for path in candidates}:
            raise ValueError
        roots = (get_sessions_dir().resolve(), (get_codex_home() / "archived_sessions").resolve())
        if not any(selected.is_relative_to(root) for root in roots):
            raise ValueError
        metadata = read_session_metadata(selected)
        if metadata.id != session_id or not metadata.canonical_history:
            raise ValueError
        return selected
    except (OSError, ValueError, CodexRpcError) as exc:
        raise AmbiguousSessionError(
            "Multiple rollout files have this UUID. Codex could not verify its selected canonical rollout; resolve the duplicate before resuming or restoring."
        ) from exc


@dataclass
class SessionCatalog:
    sessions: list[SessionItem] = field(default_factory=list)
    skipped_rollouts: int = 0
    skipped_index_records: int = 0
    unreadable_sources: int = 0


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        return 0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except (ValueError, OverflowError, OSError):
        return 0


def format_relative_time(iso_str: str) -> str:
    stamp = _timestamp(iso_str)
    if not stamp:
        return "Unknown"
    seconds = max(0, int(datetime.now(timezone.utc).timestamp() - stamp))
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 604800:
        return f"{seconds // 86400}d ago"
    return datetime.fromtimestamp(stamp, timezone.utc).strftime("%b %d, %Y")


def _workspace_key(value: str | Path) -> str:
    return os.path.normcase(os.path.normpath(str(Path(value).expanduser().resolve())))


def discover_sessions(*, include_archived: bool = False) -> SessionCatalog:
    """Read titles from the index and identities/workspaces from rollout headers.

    Index corruption does not hide unindexed rollout files. Invalid headers are
    counted rather than guessed, and duplicate UUID paths remain nonmutable.
    """
    catalog = SessionCatalog()
    index: dict[str, dict] = {}
    try:
        with get_session_index_path().open("r", encoding="utf-8-sig") as source:
            for line in source:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise TypeError
                    sid = canonical_session_id(entry.get("id"))
                    if sid not in index or _timestamp(entry.get("updated_at")) >= _timestamp(
                        index[sid].get("updated_at")
                    ):
                        index[sid] = entry
                except (ValueError, TypeError):
                    catalog.skipped_index_records += 1
    except FileNotFoundError:
        pass
    except (OSError, UnicodeDecodeError):
        catalog.unreadable_sources += 1
    roots = [(get_sessions_dir(), False)]
    if include_archived:
        roots.append((get_codex_home() / "archived_sessions", True))
    found: dict[str, SessionItem] = {}
    seen_paths: set[Path] = set()
    for root, archived in roots:
        if not root.exists():
            continue
        for candidate in root.rglob("rollout-*.jsonl*"):
            if not candidate.name.endswith((".jsonl", ".jsonl.zst")):
                continue
            path = candidate.resolve()
            if path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                # Do not follow a link into a different workspace/private tree.
                path.relative_to(root.resolve())
                meta = read_session_metadata(path)
                stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
            except (OSError, ValueError):
                catalog.skipped_rollouts += 1
                continue
            entry = index.get(meta.id, {})
            updated = max((stamp, entry.get("updated_at"), meta.timestamp), key=_timestamp)
            title = entry.get("thread_name")
            if not isinstance(title, str) or not title.strip():
                title = "Untitled session"
            item = SessionItem(
                meta.id,
                title,
                updated or "",
                format_relative_time(updated or ""),
                path,
                meta.provider or "Unknown",
                meta.cwd,
                archived,
            )
            previous = found.get(meta.id)
            if previous is not None:
                paths = previous.duplicate_paths or (previous.rollout_path,)
                if _timestamp(item.updated_at) > _timestamp(previous.updated_at):
                    item.duplicate_paths = (*paths, path)
                    found[meta.id] = item
                else:
                    previous.duplicate_paths = (*paths, path)
            else:
                found[meta.id] = item
    # Retain index-only entries so a missing rollout is visible to the user.
    for sid, entry in index.items():
        if sid in found:
            continue
        title = entry.get("thread_name")
        stamp = entry.get("updated_at")
        found[sid] = SessionItem(
            sid,
            title if isinstance(title, str) and title else "Untitled session",
            stamp if isinstance(stamp, str) else "",
            format_relative_time(stamp),
            None,
            "Unknown",
        )
    catalog.sessions = sorted(
        found.values(), key=lambda item: (_timestamp(item.updated_at), item.id), reverse=True
    )
    return catalog


def filter_sessions(
    sessions: list[SessionItem], *, search: str | None = None, workspace: str | Path | None = None
) -> list[SessionItem]:
    query = (search or "").strip().casefold()
    directory = _workspace_key(workspace) if workspace is not None else None
    return [
        item
        for item in sessions
        if (not query or query in item.id.casefold() or query in item.thread_name.casefold())
        and (directory is None or (item.cwd is not None and _workspace_key(item.cwd) == directory))
    ]


def list_sessions(
    limit: int | None = None,
    *,
    offset: int = 0,
    search: str | None = None,
    workspace: str | Path | None = None,
    include_archived: bool = False,
) -> list[SessionItem]:
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("Session page bounds must be nonnegative.")
    items = filter_sessions(
        discover_sessions(include_archived=include_archived).sessions,
        search=search,
        workspace=workspace,
    )
    return items[offset:] if limit is None else items[offset : offset + limit]


def find_session_by_id_or_title(
    query: str, *, workspace: str | Path | None = None, include_archived: bool = False
) -> SessionItem | None:
    query = query.strip().casefold()
    if not query:
        raise ValueError("Enter a session UUID, UUID prefix, or title.")
    items = list_sessions(workspace=workspace, include_archived=include_archived)
    exact = [item for item in items if item.id == query]
    matches = (
        exact
        or [item for item in items if item.id.startswith(query)]
        or [item for item in items if query in item.thread_name.casefold()]
    )
    if len(matches) > 1:
        raise AmbiguousSessionError(
            f"{len(matches)} sessions match. Choose a complete UUID from cmgr sessions."
        )
    return matches[0] if matches else None
