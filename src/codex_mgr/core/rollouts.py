"""Read session identity without trusting a shortened filename suffix."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

import zstandard

_UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_FILENAME = re.compile(rf"^rollout-.+-(?P<id>{_UUID})\.jsonl(?:\.zst)?$")
_HEADER_BYTES = 1_048_576
_COMPRESSED_PREFIX_BYTES = 4_194_304
_WINDOW_BYTES = 67_108_864  # The backend passes bytes to ZSTD_DCtx_setMaxWindowSize.


class SessionIdentityError(ValueError):
    pass


class CanonicalHistoryError(ValueError):
    def __init__(self):
        super().__init__(
            "This history requires Codex's canonical store workflow for backup or restore."
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
    compressed: bool = False


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
        path.name.endswith(".jsonl.zst"),
    )


def _read_header(source, path):
    remaining = _HEADER_BYTES
    while True:
        line = source.readline(remaining + 1)
        remaining -= len(line)
        if not line or remaining < 0:
            raise SessionIdentityError("Session metadata is missing or too large.")
        if not line.strip():
            continue
        try:
            return metadata_from_record(json.loads(line.decode("utf-8-sig")), path)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionIdentityError("Session metadata is invalid.") from exc


class _CompressedPrefix:
    def __init__(self, source):
        self.source = source
        self.remaining = _COMPRESSED_PREFIX_BYTES

    def read(self, size):
        if self.remaining <= 0:
            raise SessionIdentityError("Compressed session metadata exceeds the read limit.")
        data = self.source.read(min(size, self.remaining))
        self.remaining -= len(data)
        return data


def read_session_metadata(path: Path) -> SessionMetadata:
    """Read a bounded plain/compressed header without materializing durable files.

    A readable header establishes identity, not integrity of the entire history.
    """
    with path.open("rb") as source:
        if not path.name.endswith(".jsonl.zst"):
            return _read_header(source, path)
        try:
            decoder = zstandard.ZstdDecompressor(max_window_size=_WINDOW_BYTES).stream_reader(
                _CompressedPrefix(source),
                read_size=65_536,
                read_across_frames=True,
                closefd=False,
            )
            with io.BufferedReader(decoder) as decoded:
                return _read_header(decoded, path)
        except zstandard.ZstdError as exc:
            raise SessionIdentityError("Compressed session metadata is invalid.") from exc
