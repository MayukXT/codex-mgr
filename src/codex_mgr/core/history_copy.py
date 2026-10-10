"""Independent history copies with complete snapshots and collision-safe publication."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4

from codex_mgr.core.bridge import BridgeError, sanitize_history_record
from codex_mgr.core.history_lineage import (
    HistoryLineage,
    LineageLimits,
    index_history_rollouts,
    resolve_history_lineage,
    validate_history_record,
)
from codex_mgr.core.history_stream import (
    HistoryReadError,
    iter_history_records,
    open_history_source,
)
from codex_mgr.core.rollouts import (
    canonical_session_id,
    read_session_metadata,
    rollout_id_from_filename,
    thread_id_from_filename,
)
from codex_mgr.core.session_lock import session_write_guards
from codex_mgr.utils.files import FileConflictError, atomic_write, is_linked_path
from codex_mgr.utils.json_edits import JsonEdits
from codex_mgr.utils.paths import get_codex_home

_DIGEST = re.compile(r"[a-f0-9]{64}")
_PROVIDER = re.compile(r"[A-Za-z0-9_-]{1,128}")
_JOURNAL_LIMIT = 262_144


@dataclass(frozen=True)
class HistoryCopyLimits:
    lineage: LineageLimits = field(default_factory=LineageLimits)
    source_bytes: int = 1024 * 1024 * 1024
    candidate_bytes: int = 1024 * 1024 * 1024

    def __post_init__(self):
        if not isinstance(self.lineage, LineageLimits) or any(
            not isinstance(value, int) or isinstance(value, bool) or value <= 0
            for value in (self.source_bytes, self.candidate_bytes)
        ):
            raise HistoryReadError("Invalid history copy limits.")


@dataclass(frozen=True)
class HistoryCopyInfo:
    id: str
    original_id: str
    state: str
    sanitized_count: int
    source_count: int
    target: Path = field(repr=False)
    provider: str
    target_present: bool
    review_digest: str = field(repr=False)


@dataclass(frozen=True)
class HistoryCopyEntry:
    id: str
    status: str


def _safe(path: Path, home: Path) -> Path:
    """Check every existing component before any managed storage access."""
    path = Path(os.path.abspath(path))
    try:
        relative = path.relative_to(home)
        if not relative.parts or relative.parts[0] not in {
            "backups",
            "sessions",
            "archived_sessions",
        }:
            raise ValueError
        component = home
        for part in relative.parts:
            component /= part
            if (component.exists() or component.is_symlink()) and is_linked_path(component):
                raise ValueError
        path.resolve().relative_to(home)
    except (OSError, ValueError) as exc:
        raise HistoryReadError("History copy storage is unavailable or unsafe.") from exc
    return path


def _relative(value, roots):
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 4096
        or "\\" in value
        or ":" in value
    ):
        raise HistoryReadError("Invalid history copy recovery path.")
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts) or parts[0] not in roots:
        raise HistoryReadError("Invalid history copy recovery path.")
    return Path(*PurePosixPath(value).parts)


def _folder(identity: str, home: Path) -> Path:
    return _safe(home / "backups" / "history-copies" / canonical_session_id(identity), home)


def _measure(path: Path, limit: int, target=None, *, single_link=False) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    with open_history_source(path) as source:
        if single_link and os.fstat(source.fileno()).st_nlink != 1:
            raise HistoryReadError("Linked history recovery files are not supported.")
        while chunk := source.read(min(1_048_576, limit - size + 1)):
            size += len(chunk)
            if size > limit:
                raise HistoryReadError("History copy exceeds its byte limit.")
            digest.update(chunk)
            if target is not None:
                target.write(chunk)
    return digest.hexdigest(), size


def _copy(source: Path, destination: Path, limit: int) -> tuple[str, int]:
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with destination.open("xb") as target:
        os.chmod(destination, 0o600)
        result = _measure(source, limit, target)
        target.flush()
        os.fsync(target.fileno())
    return result


def _sync_directory(path):
    # Windows Python has no directory-flush primitive. Process-crash recovery
    # is supported; storage/controller power-loss durability is not promised.
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _header(prefix: bytes, identity: str, original: str, provider: str) -> bytes:
    rendered = []
    for raw in prefix.splitlines(keepends=True):
        if not raw.strip():
            rendered.append(raw)
            continue
        editor = JsonEdits(raw.decode("utf-8"))
        editor.set(("payload", "id"), identity)
        editor.set(("payload", "history_mode"), "legacy")
        editor.set(("payload", "history_base"), None)
        editor.set(("payload", "forked_from_id"), original)
        editor.set(("payload", "forked_from_ordinal_exclusive"), None)
        editor.set(("payload", "model_provider"), provider)
        rendered.append(editor.render().encode("utf-8"))
    return b"".join(rendered)


def _journal(folder, record, *, expected):
    encoded = json.dumps(record, separators=(",", ":")).encode("utf-8")
    if len(encoded) > _JOURNAL_LIMIT:
        raise HistoryReadError("History copy recovery record exceeds its limit.")
    atomic_write(_safe(folder / "journal.json", get_codex_home()), encoded, expected=expected)
    _sync_directory(folder)
    return encoded


def _nonnegative(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_candidate(path, record, limits):
    metadata = read_session_metadata(path)
    if (
        metadata.id != record["id"]
        or metadata.provider != record["provider"]
        or metadata.canonical_history
    ):
        raise HistoryReadError("History copy recovery identity check failed.")
    seen = False
    for count, raw in enumerate(iter_history_records(path, limits=limits.lineage.history), 1):
        if count > limits.lineage.records:
            raise HistoryReadError("History copy exceeds its record limit.")
        if not raw.strip():
            continue
        envelope = validate_history_record(raw)
        if not seen:
            payload = envelope["payload"]
            if (
                envelope["type"] != "session_meta"
                or not isinstance(payload, dict)
                or payload.get("history_mode") != "legacy"
                or payload.get("history_base") is not None
                or payload.get("forked_from_id") != record["original_id"]
                or payload.get("forked_from_ordinal_exclusive") is not None
            ):
                raise HistoryReadError("History copy recovery identity check failed.")
            seen = True
        elif envelope["type"] == "session_meta":
            raise HistoryReadError("History copy contains ambiguous metadata.")


def _bundle(identity: str, home: Path, limits: HistoryCopyLimits):
    """Verify the entire private bundle before recovery or publication."""
    folder = _folder(identity, home)
    journal = _safe(folder / "journal.json", home)
    with open_history_source(journal) as source:
        if os.fstat(source.fileno()).st_nlink != 1:
            raise HistoryReadError("Linked history recovery files are not supported.")
        encoded = source.read(_JOURNAL_LIMIT + 1)
    if len(encoded) > _JOURNAL_LIMIT:
        raise HistoryReadError("History copy recovery record exceeds its limit.")
    try:
        record = JsonEdits(encoded.decode("utf-8")).value
        if (
            not isinstance(record, dict)
            or set(record)
            != {
                "version",
                "state",
                "original_id",
                "id",
                "provider",
                "fingerprint",
                "target",
                "candidate",
                "sources",
                "sanitized_count",
            }
            or type(record["version"]) is not int
            or record["version"] != 1
            or record["state"] not in {"prepared", "published"}
            or canonical_session_id(record["id"]) != identity
            or canonical_session_id(record["original_id"]) == identity
            or not isinstance(record["provider"], str)
            or _PROVIDER.fullmatch(record["provider"]) is None
            or not isinstance(record["fingerprint"], str)
            or _DIGEST.fullmatch(record["fingerprint"]) is None
            or not _nonnegative(record["sanitized_count"])
            or not isinstance(record["sources"], list)
            or not 0 < len(record["sources"]) <= limits.lineage.segments
        ):
            raise ValueError
        target = _safe(home / _relative(record["target"], {"sessions"}), home)
        if target.name.endswith(".zst") or rollout_id_from_filename(target) != identity:
            raise ValueError
        entries = [record["candidate"], *record["sources"]]
        if not isinstance(entries[0], dict) or set(entries[0]) != {"sha256", "size"}:
            raise ValueError
        total, paths = 0, set()
        for index, entry in enumerate(entries):
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("sha256"), str)
                or _DIGEST.fullmatch(entry["sha256"]) is None
                or not _nonnegative(entry.get("size"))
            ):
                raise ValueError
            if index:
                if set(entry) != {"path", "thread_id", "rollout_id", "sha256", "size"}:
                    raise ValueError
                relative = _relative(entry["path"], {"sessions", "archived_sessions"})
                if entry["path"] in paths:
                    raise ValueError
                paths.add(entry["path"])
                path = _safe(folder / "sources" / relative, home)
                total += entry["size"]
                if total > limits.source_bytes:
                    raise ValueError
                canonical_session_id(entry["thread_id"])
                if rollout_id_from_filename(path) != canonical_session_id(entry["rollout_id"]):
                    raise ValueError
                limit = limits.source_bytes
            else:
                path, limit = _safe(folder / "candidate.jsonl", home), limits.candidate_bytes
            if entry["size"] > limit:
                raise ValueError
            digest, size = _measure(path, entry["size"], single_link=True)
            if (digest, size) != (entry["sha256"], entry["size"]):
                raise ValueError
        sources_home = folder / "sources"
        root = sources_home / _relative(
            record["sources"][-1]["path"], {"sessions", "archived_sessions"}
        )
        lineage = resolve_history_lineage(root, home=sources_home, limits=limits.lineage)
        if (
            lineage.thread_id != record["original_id"]
            or len(lineage.segments) != len(record["sources"])
            or lineage.fingerprint() != record["fingerprint"]
        ):
            raise ValueError
        for segment, entry in zip(lineage.segments, record["sources"]):
            if (
                segment.path.relative_to(sources_home).as_posix(),
                segment.metadata.id,
                segment.metadata.rollout_id,
            ) != (entry["path"], entry["thread_id"], entry["rollout_id"]):
                raise ValueError
        _validate_candidate(folder / "candidate.jsonl", record, limits)
    except (ValueError, TypeError, KeyError, UnicodeDecodeError, RecursionError) as exc:
        raise HistoryReadError("History copy recovery integrity check failed.") from exc
    return folder, record, encoded, target


def _info(record, target, encoded):
    return HistoryCopyInfo(
        record["id"],
        record["original_id"],
        record["state"],
        record["sanitized_count"],
        len(record["sources"]),
        target,
        record["provider"],
        target.exists(),
        hashlib.sha256(encoded).hexdigest(),
    )


def list_history_copies(
    *, limit: int | None = None, offset: int = 0, limits: HistoryCopyLimits | None = None
) -> list[HistoryCopyEntry]:
    """List UUID folders without reading transcripts or claiming bundle integrity."""
    if (
        type(offset) is not int
        or offset < 0
        or (limit is not None and (type(limit) is not int or limit <= 0))
    ):
        raise ValueError("Choose a positive page size and a nonnegative offset.")
    home = get_codex_home()
    limits = limits or HistoryCopyLimits()
    folder = _safe(home / "backups" / "history-copies", home)
    if not folder.exists():
        return []
    result = []
    with os.scandir(folder) as entries:
        for count, entry in enumerate(entries, 1):
            if count > limits.lineage.directory_entries:
                raise HistoryReadError("History copy inventory exceeds its directory entry limit.")
            try:
                identity = canonical_session_id(entry.name)
            except ValueError:
                continue
            if identity != entry.name:
                continue
            status = (
                "unchecked"
                if not is_linked_path(Path(entry.path)) and entry.is_dir(follow_symlinks=False)
                else "unsafe"
            )
            result.append(HistoryCopyEntry(identity, status))
    result.sort(key=lambda item: item.id)
    return result[offset : None if limit is None else offset + limit]


def read_history_copy(identity: str, *, limits: HistoryCopyLimits | None = None) -> HistoryCopyInfo:
    """Inspect verified recovery metadata without exposing transcript contents."""
    identity = canonical_session_id(identity)
    _, record, encoded, target = _bundle(identity, get_codex_home(), limits or HistoryCopyLimits())
    return _info(record, target, encoded)


def _has_prefix(path, candidate, size):
    with open_history_source(path) as current, open_history_source(candidate) as expected:
        if any(os.fstat(handle.fileno()).st_nlink != 1 for handle in (current, expected)):
            raise HistoryReadError("Linked history recovery files are not supported.")
        remaining = size
        while remaining:
            count = min(remaining, 1_048_576)
            a, b = current.read(count), expected.read(count)
            if a != b or len(b) != count:
                return False
            remaining -= count
    return True


def _identity_exists(identity, home, limits):
    # Canonical replacement names carry both logical and immutable UUIDs.
    # Reserve either namespace instead of selecting a duplicate by timestamp.
    index = index_history_rollouts(home, limits.lineage.directory_entries)
    return any(identity in path.name for paths in index.values() for path in paths)


def _root_rollouts(index, identity):
    return frozenset(
        immutable
        for immutable, paths in index.items()
        if any(thread_id_from_filename(path) == identity for path in paths)
    )


def _publish(folder, record, encoded, target, home, limits):
    candidate = _safe(folder / "candidate.jsonl", home)
    if target.exists():
        if not _has_prefix(target, candidate, record["candidate"]["size"]):
            raise FileConflictError("The new session path changed; existing content was preserved.")
    else:
        if _identity_exists(record["id"], home, limits):
            raise FileConflictError(
                "A session already uses this identity; its history was preserved."
            )
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, name = tempfile.mkstemp(
            prefix=".cmgr-history-", suffix=".tmp", dir=target.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.chmod(temporary, 0o600)
                digest, size = _measure(candidate, limits.candidate_bytes, output, single_link=True)
                if (digest, size) != (record["candidate"]["sha256"], record["candidate"]["size"]):
                    raise FileConflictError(
                        "The prepared history copy changed; publication stopped."
                    )
                output.flush()
                os.fsync(output.fileno())
            # A separate adjacent inode protects the private candidate from native
            # appends. link() creates the name exclusively, including race collisions.
            try:
                os.link(temporary, target, follow_symlinks=False)
            except FileExistsError as exc:
                raise FileConflictError(
                    "The new session path already exists; it was preserved."
                ) from exc
            except OSError as exc:
                raise HistoryReadError(
                    "This storage could not publish the independent history copy."
                ) from exc
            _sync_directory(target.parent)
        finally:
            temporary.unlink(missing_ok=True)
    if record["state"] != "published":
        record["state"] = "published"
        encoded = _journal(folder, record, expected=encoded)
    return target, encoded


def recover_history_copy(
    identity: str,
    *,
    publish: bool = False,
    allow_reasoning_loss: bool = False,
    expected_digest: str | None = None,
    limits: HistoryCopyLimits | None = None,
) -> HistoryCopyInfo:
    """Explicitly settle/re-publish a verified independent copy; never launch Codex.

    A missing target is kept missing unless publish=True. Existing native appends
    are preserved when the entire frozen prefix still matches. Source locations
    may since have moved or disappeared; recovery uses the complete private bundle.
    """
    identity, home = canonical_session_id(identity), get_codex_home()
    if type(publish) is not bool:
        raise ValueError("Choose explicit history publication controls.")
    limits = limits or HistoryCopyLimits()
    folder, record, encoded, target = _bundle(identity, home, limits)
    if expected_digest is not None and hashlib.sha256(encoded).hexdigest() != expected_digest:
        raise FileConflictError("History recovery changed after inspection; review it again.")
    if publish and record["sanitized_count"] and allow_reasoning_loss is not True:
        raise BridgeError("This copy requires explicit consent to remove encrypted items.")
    with session_write_guards(((identity, target),)):
        folder, current, current_encoded, target = _bundle(identity, home, limits)
        if current_encoded != encoded:
            raise FileConflictError("History recovery changed after inspection; review it again.")
        if publish or target.exists():
            target, current_encoded = _publish(
                folder, current, current_encoded, target, home, limits
            )
        return _info(current, target, current_encoded)


@dataclass
class PreparedHistoryCopy:
    id: str
    original_id: str
    expected_fingerprint: str
    sanitized_count: int
    changed_records: int
    lineage: HistoryLineage = field(repr=False)
    target: Path = field(repr=False)
    candidate: Path = field(repr=False)
    candidate_digest: str = field(repr=False)
    candidate_size: int = field(repr=False)
    provider: str = field(repr=False)
    limits: HistoryCopyLimits = field(repr=False)
    root_rollouts: frozenset[str] = field(repr=False)
    _snapshotted: bool = field(default=False, repr=False)

    def _current(self):
        if get_codex_home() != self.lineage.home:
            raise FileConflictError("The active history home changed; prepare the copy again.")
        root = self.lineage.segments[-1].metadata.rollout_id
        index = index_history_rollouts(self.lineage.home, self.limits.lineage.directory_entries)
        if _root_rollouts(index, self.original_id) != self.root_rollouts:
            raise FileConflictError("Canonical root selection changed; review the session again.")
        candidates = index.get(root, [])
        if len(candidates) != 1:
            raise FileConflictError("The selected history source moved ambiguously or disappeared.")
        current = resolve_history_lineage(
            candidates[0], home=self.lineage.home, limits=self.limits.lineage
        )
        if current.fingerprint() != self.expected_fingerprint:
            raise FileConflictError("History changed after preparation; review it again.")
        return current

    @contextmanager
    def _guard(self):
        current = self._current()
        _safe(self.target, self.lineage.home)
        with session_write_guards(
            [
                *((segment.metadata.id, segment.path) for segment in current.segments),
                (self.id, self.target),
            ]
        ):
            _safe(self.target, self.lineage.home)
            current = self._current()
            yield current

    def snapshot(self) -> HistoryCopyInfo:
        """Save and verify all raw sources and the candidate before any publication."""
        if self._snapshotted:
            return read_history_copy(self.id, limits=self.limits)
        home, folder = self.lineage.home, _folder(self.id, self.lineage.home)
        with self._guard() as current:
            if _identity_exists(self.id, home, self.limits):
                raise FileConflictError(
                    "A session already uses this identity; its history was preserved."
                )
            _safe(folder, home).mkdir(parents=True, exist_ok=False, mode=0o700)
            sources, remaining = [], self.limits.source_bytes
            for segment in current.segments:
                relative = segment.path.relative_to(home)
                digest, size = _copy(
                    segment.path, _safe(folder / "sources" / relative, home), remaining
                )
                remaining -= size
                sources.append(
                    {
                        "path": relative.as_posix(),
                        "thread_id": segment.metadata.id,
                        "rollout_id": segment.metadata.rollout_id,
                        "sha256": digest,
                        "size": size,
                    }
                )
            digest, size = _copy(
                self.candidate, _safe(folder / "candidate.jsonl", home), self.limits.candidate_bytes
            )
            if (digest, size) != (self.candidate_digest, self.candidate_size):
                raise FileConflictError("The prepared history copy changed; snapshotting stopped.")
            remaining = self.limits.source_bytes
            for segment, entry in zip(current.segments, sources):
                digest, size = _measure(segment.path, remaining)
                remaining -= size
                if (digest, size) != (entry["sha256"], entry["size"]):
                    raise FileConflictError(
                        "History changed during snapshotting; publication stopped."
                    )
            self._current()
            record = {
                "version": 1,
                "state": "prepared",
                "original_id": self.original_id,
                "id": self.id,
                "provider": self.provider,
                "fingerprint": self.expected_fingerprint,
                "target": self.target.relative_to(home).as_posix(),
                "candidate": {"sha256": self.candidate_digest, "size": self.candidate_size},
                "sources": sources,
                "sanitized_count": self.sanitized_count,
            }
            encoded = _journal(folder, record, expected=None)
            _bundle(self.id, home, self.limits)
            self._snapshotted = True
        return _info(record, self.target, encoded)

    def publish(self, *, allow_reasoning_loss: bool = False) -> Path:
        """Publish a reviewed independent UUID, preserving every original and index."""
        if self.sanitized_count and allow_reasoning_loss is not True:
            raise BridgeError("This copy requires explicit consent to remove encrypted items.")
        self.snapshot()
        with self._guard():
            folder, record, encoded, target = _bundle(self.id, self.lineage.home, self.limits)
            return _publish(folder, record, encoded, target, self.lineage.home, self.limits)[0]


@contextmanager
def prepare_history_copy(
    path: Path,
    target_provider_id: str,
    *,
    strip_reasoning: bool = False,
    preserve_reasoning: Callable[[dict], bool] | None = None,
    limits: HistoryCopyLimits | None = None,
) -> Iterator[PreparedHistoryCopy]:
    """Validate a selected canonical root and stage an independent legacy history.

    The caller must have selected replacement roots through native state. All
    inherited records/compaction metadata remain exact apart from reviewed opaque
    item edits. This scope alone does not change native history or configuration.
    """
    if not isinstance(target_provider_id, str) or _PROVIDER.fullmatch(target_provider_id) is None:
        raise BridgeError("Choose a valid Codex connection identifier.")
    if type(strip_reasoning) is not bool or (
        preserve_reasoning is not None and not callable(preserve_reasoning)
    ):
        raise BridgeError("Choose explicit history conversion controls.")
    limits = limits or HistoryCopyLimits()
    home = get_codex_home()
    lineage = resolve_history_lineage(path, home=home, limits=limits.lineage)
    root_rollouts = _root_rollouts(
        index_history_rollouts(home, limits.lineage.directory_entries), lineage.thread_id
    )
    if lineage.segments[-1].metadata.rollout_id not in root_rollouts:
        raise HistoryReadError("Canonical root filename does not match its logical session.")
    expected = lineage.fingerprint()
    identity = str(uuid4())
    if identity == lineage.thread_id:
        raise FileConflictError("The new session identity collided; prepare the copy again.")
    now = datetime.now(timezone.utc)
    target = _safe(
        home
        / "sessions"
        / now.strftime("%Y/%m/%d")
        / f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{identity}.jsonl",
        home,
    )
    storage = _safe(home / "backups" / "history-copies", home)
    storage.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix=".prepare-", dir=storage) as temporary:
        candidate = Path(temporary) / "candidate.jsonl"
        digest, size, sanitized, changed = hashlib.sha256(), 0, 0, 0
        with candidate.open("xb") as output:
            os.chmod(candidate, 0o600)
            for index, raw in enumerate(lineage.iter_records()):
                converted = raw
                if index == 0:
                    converted = _header(raw, identity, lineage.thread_id, target_provider_id)
                elif raw.strip() and (
                    strip_reasoning
                    or preserve_reasoning is not None
                    or lineage.segments[-1].metadata.provider != target_provider_id
                ):
                    editor = JsonEdits(raw.decode("utf-8"))
                    sanitized += sanitize_history_record(
                        editor,
                        None if strip_reasoning else preserve_reasoning,
                        reasoning_only=not strip_reasoning
                        and lineage.segments[-1].metadata.provider == target_provider_id,
                    )
                    converted = editor.render().encode("utf-8") if editor.edits else raw
                size += len(converted)
                if size > limits.candidate_bytes:
                    raise HistoryReadError("History copy exceeds its candidate byte limit.")
                changed += converted != raw
                digest.update(converted)
                output.write(converted)
            output.flush()
            os.fsync(output.fileno())
        if lineage.fingerprint() != expected:
            raise FileConflictError("History changed during preparation; review it again.")
        _validate_candidate(
            candidate,
            {"id": identity, "original_id": lineage.thread_id, "provider": target_provider_id},
            limits,
        )
        yield PreparedHistoryCopy(
            identity,
            lineage.thread_id,
            expected,
            sanitized,
            changed,
            lineage,
            target,
            candidate,
            digest.hexdigest(),
            size,
            target_provider_id,
            limits,
            root_rollouts,
        )
