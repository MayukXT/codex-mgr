"""File mutation primitives shared by settings, config, and recovery."""

from __future__ import annotations

import hashlib
import os
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

_UNSET = object()


class FileConflictError(RuntimeError):
    """Another writer owns the mutation lock or changed the expected file."""


def read_optional(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


@contextmanager
def mutation_lock(path: Path) -> Iterator[None]:
    """Nonblocking OS lock for manager writers; a filename is not lock state.

    Noncooperating editors do not honor this advisory lock. Expected-content
    checks detect their edits before replacement.
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = path.with_name(path.name + ".cmgr.lock")
    with lock_path.open("a+b") as handle:
        os.chmod(lock_path, 0o600)
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise FileConflictError("Another manager operation is editing this file.") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def atomic_write(path: Path, data: bytes, *, expected: bytes | None | object = _UNSET) -> None:
    """Flush an adjacent temp file, check for edits, then replace.

    `expected=None` requires a missing destination. Unix files are mode 0600;
    Windows access follows the containing directory's ACL.
    """
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.chmod(temporary, 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if expected is not _UNSET and read_optional(path) != expected:
            raise FileConflictError("File changed during this operation; reload before retrying.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def create_snapshot(source: Path, folder: Path, *, suffix: str, prefix: str = "") -> Path:
    """Copy bytes into a unique snapshot and record a corruption checksum."""
    data = source.read_bytes()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    destination = folder / f"{prefix}{timestamp}_{uuid.uuid4().hex}{suffix}"
    atomic_write(destination, data, expected=None)
    digest = hashlib.sha256(data).hexdigest().encode("ascii") + b"\n"
    atomic_write(destination.with_name(destination.name + ".sha256"), digest, expected=None)
    return destination


def read_snapshot(path: Path) -> bytes:
    """Verify new snapshots; legacy snapshots have no corruption checksum."""
    data = path.read_bytes()
    checksum = path.with_name(path.name + ".sha256")
    if checksum.exists():
        digest = hashlib.sha256(data).hexdigest()
        if checksum.read_text(encoding="ascii").strip() != digest:
            raise ValueError("Snapshot integrity check failed; original state was preserved.")
    return data
