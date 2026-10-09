"""Session catalog, indexer, and search engine.

Parses ~/.codex/session_index.jsonl and locates rollout files with human-friendly
thread names, relative timestamps, and provider origin badges.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from codex_mgr.core.bridge import inspect_session_provider
from codex_mgr.utils.paths import get_session_index_path, get_sessions_dir


@dataclass
class SessionItem:
    """Represents an indexed Codex conversation session."""

    id: str
    thread_name: str
    updated_at: str
    relative_time: str
    rollout_path: Optional[Path]
    origin_provider: str


def format_relative_time(iso_str: str) -> str:
    """Convert an ISO timestamp into human-readable relative time (e.g. '2 hours ago')."""
    try:
        clean_iso = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(clean_iso)
        now = datetime.now(timezone.utc)
        diff = now - dt

        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "just now"
        if seconds < 3600:
            m = seconds // 60
            return f"{m}m ago"
        if seconds < 86400:
            h = seconds // 3600
            return f"{h}h ago"
        if seconds < 604800:
            d = seconds // 86400
            return "yesterday" if d == 1 else f"{d}d ago"
        return dt.strftime("%b %d, %Y")
    except Exception:
        return "recently"


def list_sessions(limit: int = 50) -> List[SessionItem]:
    """List recorded Codex sessions sorted by most recently updated."""
    index_path = get_session_index_path()
    sessions_dir = get_sessions_dir()

    # 1. Build map of session UUID -> rollout file path
    rollout_map: Dict[str, Path] = {}
    if sessions_dir.exists():
        for r_path in sessions_dir.glob("**/*rollout-*.jsonl"):
            # rollout files end with <uuid>.jsonl
            stem_parts = r_path.stem.split("-")
            if stem_parts:
                sid = stem_parts[-1]
                rollout_map[sid] = r_path

    # 2. Parse session_index.jsonl
    items: List[SessionItem] = []
    if index_path.exists():
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                for line in f:
                    line_str = line.strip()
                    if not line_str:
                        continue
                    try:
                        entry = json.loads(line_str)
                        sid = entry.get("id")
                        if not sid:
                            continue

                        title = entry.get("thread_name") or "Untitled Conversation"
                        updated_at = entry.get("updated_at") or ""
                        rel_time = format_relative_time(updated_at)
                        r_path = rollout_map.get(sid)

                        origin = inspect_session_provider(r_path) if r_path else "openai"

                        items.append(SessionItem(
                            id=sid,
                            thread_name=title,
                            updated_at=updated_at,
                            relative_time=rel_time,
                            rollout_path=r_path,
                            origin_provider=origin or "openai",
                        ))
                    except Exception:
                        continue
        except Exception:
            pass

    # Sort descending by updated_at
    items.sort(key=lambda s: s.updated_at, reverse=True)
    return items[:limit]


def find_session_by_id_or_title(query: str) -> Optional[SessionItem]:
    """Find a session matching either a UUID prefix or title substring."""
    all_sessions = list_sessions(limit=100)
    q_lower = query.lower().strip()

    # Exact ID match
    for s in all_sessions:
        if s.id.lower() == q_lower or s.id.lower().startswith(q_lower):
            return s

    # Title match
    for s in all_sessions:
        if q_lower in s.thread_name.lower():
            return s

    return None
