"""Read frozen canonical history ranges without modifying sources or native state."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

from codex_mgr.core.history_stream import HistoryLimits, HistoryReadError, iter_history_records
from codex_mgr.core.rollouts import (
    SessionMetadata,
    canonical_session_id,
    metadata_from_record,
    rollout_id_from_filename,
)
from codex_mgr.utils.files import FileConflictError, is_linked_path
from codex_mgr.utils.json_edits import JsonEdits
from codex_mgr.utils.paths import get_codex_home

_U64_MAX = (1 << 64) - 1
_HEADER_BYTES = 1_048_576
_RECORD_TYPES = {
    "session_meta",
    "response_item",
    "inter_agent_communication",
    "inter_agent_communication_metadata",
    "compacted",
    "turn_context",
    "token_usage_record",
    "world_state",
    "retained_context",
    "security_risk_score",
    "event_msg",
    "realtime_item",
}


class LineageError(HistoryReadError):
    pass


@dataclass(frozen=True)
class LineageLimits:
    history: HistoryLimits = field(default_factory=HistoryLimits)
    segments: int = 128
    directory_entries: int = 100_000
    decoded_bytes: int = 1024 * 1024 * 1024
    records: int = 2_000_000

    def __post_init__(self):
        if not isinstance(self.history, HistoryLimits) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for key, value in vars(self).items()
            if key != "history"
        ):
            raise LineageError("Invalid history lineage limits.")


@dataclass(frozen=True)
class HistoryPosition:
    rollout_id: str
    ordinal: int
    byte_offset: int


@dataclass(frozen=True)
class HistorySegment:
    path: Path
    metadata: SessionMetadata
    metadata_prefix: bytes = field(repr=False)
    metadata_ordinal: int
    end: HistoryPosition | None


@dataclass(frozen=True)
class HistoryLineage:
    """Ordered oldest-to-current segments; metadata is checked again during replay.

    The root must already be selected by native state when it has replacements.
    Resolution alone is not whole-history validation or writer coordination.
    """

    home: Path
    segments: tuple[HistorySegment, ...]
    limits: LineageLimits

    @property
    def thread_id(self):
        return self.segments[-1].metadata.id

    def iter_records(self) -> Iterator[bytes]:
        """Yield root metadata plus exact contributing records, omitting ancestor metadata.

        Exhaust before using the result. Envelope JSON, ordinals and cutoffs are
        checked here; native payload semantics and safe publication are separate.
        """
        yield self.segments[-1].metadata_prefix
        total = count = 0
        for segment in self.segments:
            _safe_path(segment.path, self.home)
            with closing(iter_history_records(segment.path, limits=self.limits.history)) as records:
                prefix, record, header_records = _header(records)
                if prefix != segment.metadata_prefix:
                    raise FileConflictError("History metadata changed after lineage resolution.")
                offset = len(prefix)
                expected = segment.metadata_ordinal + 1
                total += offset
                count += header_records
                if total > self.limits.decoded_bytes or count > self.limits.records:
                    raise LineageError("History lineage exceeds the read limit.")
                if segment.end is not None and offset > segment.end.byte_offset:
                    raise LineageError("Inherited history cutoff does not align with a record.")
                while segment.end is None or offset < segment.end.byte_offset:
                    raw = next(records, None)
                    if raw is None:
                        if segment.end is not None:
                            raise LineageError("Inherited history cutoff is past its source.")
                        break
                    offset += len(raw)
                    total += len(raw)
                    count += 1
                    if total > self.limits.decoded_bytes or count > self.limits.records:
                        raise LineageError("History lineage exceeds the read limit.")
                    if segment.end is not None and offset > segment.end.byte_offset:
                        raise LineageError("Inherited history cutoff does not align with a record.")
                    if raw.strip():
                        record = _record(raw)
                        if record["type"] == "session_meta":
                            raise LineageError("Multiple history metadata records are ambiguous.")
                        if _u64(record.get("ordinal")) != expected:
                            raise LineageError("History has an invalid ordinal sequence.")
                        expected += 1
                    yield raw
                if segment.end is not None and expected != segment.end.ordinal:
                    raise LineageError("Inherited history ordinal and byte cutoffs disagree.")


def _u64(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _U64_MAX:
        raise LineageError("History has an invalid ordinal or cutoff.")
    return value


def _record(raw):
    try:
        record = JsonEdits(raw.decode("utf-8")).value
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        raise LineageError("History contains a malformed JSON record.") from exc
    if (
        not isinstance(record, dict)
        or not isinstance(record.get("timestamp"), str)
        or not isinstance(record.get("type"), str)
        or record.get("type") not in _RECORD_TYPES
        or "payload" not in record
    ):
        raise LineageError("History contains an unsupported record envelope.")
    return record


def _header(records):
    prefix = bytearray()
    for count, raw in enumerate(records, 1):
        if len(prefix) + len(raw) > _HEADER_BYTES:
            raise LineageError("History metadata is missing or too large.")
        prefix.extend(raw)
        if raw.strip():
            record = _record(raw)
            if record["type"] != "session_meta":
                raise LineageError("History must begin with session metadata.")
            return bytes(prefix), record, count
    raise LineageError("History metadata is missing.")


def _safe_path(path, home):
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(home)
        if not relative.parts or relative.parts[0] not in {"sessions", "archived_sessions"}:
            raise ValueError
        component = home
        for part in relative.parts:
            component /= part
            if is_linked_path(component):
                raise ValueError
        path.resolve().relative_to(home)
    except (OSError, ValueError) as exc:
        raise LineageError("History source is unavailable or outside managed storage.") from exc
    return path


def _rollout_index(home, limit):
    """Bound traversal and skip directory/file links rather than following them."""
    index = {}
    stack = []
    for name in ("sessions", "archived_sessions"):
        root = home / name
        if root.exists():
            stack.append(_safe_path(root, home))
    count = 0
    while stack:
        directory = _safe_path(stack.pop(), home)
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > limit:
                    raise LineageError("History lookup exceeds the directory entry limit.")
                path = Path(entry.path)
                if is_linked_path(path):
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(path)
                elif entry.is_file(follow_symlinks=False):
                    identity = rollout_id_from_filename(path)
                    if identity is not None:
                        index.setdefault(identity, []).append(path)
    return index


def resolve_history_lineage(
    path: Path, *, home: Path | None = None, limits: LineageLimits | None = None
) -> HistoryLineage:
    """Resolve immutable references; reject missing, duplicate or cyclic ancestry."""
    home = (home or get_codex_home()).resolve()
    limits = limits or LineageLimits()
    path = _safe_path(path, home)
    segments, seen = [], set()
    end = index = None
    header_bytes = header_count = 0
    while True:
        if len(segments) >= limits.segments:
            raise LineageError("History exceeds the lineage segment limit.")
        with closing(iter_history_records(path, limits=limits.history)) as records:
            prefix, record, header_records = _header(records)
        header_bytes += len(prefix)
        header_count += header_records
        if header_bytes > limits.decoded_bytes or header_count > limits.records:
            raise LineageError("History lineage exceeds the read limit.")
        try:
            metadata = metadata_from_record(record, path)
        except ValueError as exc:
            raise LineageError("History metadata identity is invalid.") from exc
        if (
            metadata.from_filename
            or record["payload"].get("history_mode") != "paginated"
            or rollout_id_from_filename(path) is None
        ):
            raise LineageError("History lineage requires canonical paginated metadata.")
        if metadata.rollout_id in seen:
            raise LineageError("History lineage contains a cycle.")
        seen.add(metadata.rollout_id)
        base = record["payload"].get("history_base")
        position = None
        if base is not None:
            try:
                if not isinstance(base, dict):
                    raise TypeError
                identity = canonical_session_id(base.get("thread_id"))
                ordinal, offset = (
                    _u64(base.get("end_ordinal_exclusive")),
                    _u64(base.get("end_byte_offset")),
                )
                if not 0 < ordinal < _U64_MAX or offset == 0:
                    raise ValueError
                position = HistoryPosition(identity, ordinal, offset)
            except (TypeError, ValueError) as exc:
                raise LineageError("History has an invalid inherited cutoff.") from exc
        metadata_ordinal = _u64(record.get("ordinal"))
        if metadata_ordinal != (position.ordinal if position else 0):
            raise LineageError("History metadata ordinal does not match its inherited cutoff.")
        if end is not None and end.ordinal <= metadata_ordinal:
            raise LineageError("Inherited history cutoff excludes its source metadata.")
        segments.append(HistorySegment(path, metadata, prefix, metadata_ordinal, end))
        if position is None:
            return HistoryLineage(home, tuple(reversed(segments)), limits)
        if index is None:
            try:
                index = _rollout_index(home, limits.directory_entries)
            except OSError as exc:
                raise LineageError("History lookup failed in managed storage.") from exc
        candidates = index.get(position.rollout_id, [])
        if len(candidates) != 1:
            raise LineageError("Inherited history source is missing or ambiguous.")
        path = _safe_path(candidates[0], home)
        end = position
