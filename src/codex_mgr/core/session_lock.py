"""Coordinate with Codex's native rollout writers and manager mutations."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from codex_mgr.core.rollouts import canonical_session_id
from codex_mgr.utils.files import FileConflictError, is_linked_path, mutation_lock
from codex_mgr.utils.paths import get_codex_home


@contextmanager
def _native_lock(path: Path, *, create: bool) -> Iterator[None]:
    """Rust std::fs locks use flock on Unix and LockFileEx on Windows."""
    if path.is_symlink() or (path.exists() and is_linked_path(path)):
        raise FileConflictError("Native session lock storage is linked or unsafe.")
    if path.exists() and not path.is_file():
        raise FileConflictError("Native session lock storage must be a regular file.")
    flags = (
        os.O_RDWR
        | (os.O_CREAT if create else 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise FileConflictError("Native session lock storage must be a regular file.")
        if os.name == "nt":
            import ctypes
            import msvcrt
            from ctypes import wintypes

            class Overlapped(ctypes.Structure):
                _fields_ = [
                    ("Internal", ctypes.c_size_t),
                    ("InternalHigh", ctypes.c_size_t),
                    ("Offset", wintypes.DWORD),
                    ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE),
                ]

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            lock = kernel.LockFileEx
            lock.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            lock.restype = wintypes.BOOL
            unlock = kernel.UnlockFileEx
            unlock.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.DWORD,
                wintypes.DWORD,
                ctypes.POINTER(Overlapped),
            ]
            unlock.restype = wintypes.BOOL
            state = Overlapped()
            native = msvcrt.get_osfhandle(handle.fileno())
            if not lock(native, 3, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(state)):
                raise FileConflictError(
                    "This session has an active writer. Close it before changing its saved history."
                )
            try:
                yield
            finally:
                unlock(native, 0, 0xFFFFFFFF, 0xFFFFFFFF, ctypes.byref(state))
        else:
            import fcntl

            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise FileConflictError(
                    "This session has an active writer. Close it before changing its saved history."
                ) from exc
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def session_write_guard(session_id: str, path: Path) -> Iterator[None]:
    """Hold native coordination through publication; never delete native locks.

    Supported Codex versions take coordination before opening a UUID writer
    lock. Older clients/editors require being stopped; content checks remain
    necessary because they do not participate in this protocol.
    """
    with session_write_guards(((session_id, path),)):
        yield


@contextmanager
def session_write_guards(sessions: Iterable[tuple[str, Path]]) -> Iterator[None]:
    """Acquire one coordinator for all logical source threads and target paths.

    Deduplication prevents self-conflicts for replacement rollouts. Native
    writers and compression use the metadata thread ID, rather than a distinct
    immutable rollout ID. Validate/encode outside this scope, then recheck
    content inside it before snapshots/publication. Native RPCs run after exit.
    """
    sources = tuple((canonical_session_id(identity), path.resolve()) for identity, path in sessions)
    if not sources:
        raise ValueError("Choose at least one session to coordinate.")
    folder = get_codex_home() / "thread-writer-locks"
    if folder.is_symlink() or (folder.exists() and (is_linked_path(folder) or not folder.is_dir())):
        raise FileConflictError("Native session lock storage is linked or unsafe.")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ExitStack() as stack:
        for path in sorted({path for _, path in sources}, key=str):
            stack.enter_context(mutation_lock(path))
        stack.enter_context(_native_lock(folder / ".coordination.lock", create=True))
        for identity in sorted({identity for identity, _ in sources}):
            try:
                stack.enter_context(_native_lock(folder / f"{identity}.lock", create=False))
            except FileNotFoundError:
                pass
        yield
