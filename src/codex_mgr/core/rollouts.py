"""Read session identity without trusting a shortened filename suffix."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_FILENAME = re.compile(rf"^rollout-.+-(?P<id>{_UUID})\.jsonl$")


class SessionIdentityError(ValueError):
    pass


class CanonicalHistoryError(ValueError):
    def __init__(self):
        super().__init__(
            "Paginated or inherited history requires Codex's canonical store workflow. File-only backup and restore are unavailable."
        )


def canonical_session_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(_UUID, value):
        raise SessionIdentityError("Session identity must be a complete UUID.")
    return str(UUID(value))


@dataclass(frozen=True)
class SessionMetadata:
    id: str
    provider: str | None
    cwd: str | None
    timestamp: str | None
    from_filename: bool = False
    canonical_history: bool = False


def metadata_from_record(record: object, path: Path) -> SessionMetadata:
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        raise SessionIdentityError("Rollout must begin with a session metadata record.")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        raise SessionIdentityError("Session metadata is invalid.")
    mode = payload.get("history_mode", "legacy")
    if not isinstance(mode, str) or mode not in {"legacy", "paginated"}:
        raise SessionIdentityError("Session metadata has an unsupported history mode.")
    match = _FILENAME.fullmatch(path.name)
    filename_id = canonical_session_id(match["id"]) if match else None
    raw_id = payload.get("id")
    if raw_id is None:
        if filename_id is None:
            raise SessionIdentityError("Session metadata and filename have no complete UUID.")
        session_id = filename_id
    else:
        session_id = canonical_session_id(raw_id)
        if filename_id is not None and filename_id != session_id:
            raise SessionIdentityError("Session metadata and filename identify different sessions.")

    def text(key):
        value = payload.get(key)
        return value if isinstance(value, str) and value else None

    return SessionMetadata(
        session_id,
        text("model_provider"),
        text("cwd"),
        text("timestamp"),
        raw_id is None,
        mode == "paginated" or payload.get("history_base") is not None,
    )


def read_session_metadata(path: Path) -> SessionMetadata:
    with path.open("rb") as source:
        while True:
            line = source.readline(1_048_577)
            if not line or len(line) > 1_048_576:
                raise SessionIdentityError("Session metadata is missing or too large.")
            if not line.strip():
                continue
            try:
                return metadata_from_record(json.loads(line.decode("utf-8-sig")), path)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SessionIdentityError("Session metadata is invalid.") from exc
