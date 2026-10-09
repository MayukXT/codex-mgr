"""Coordinate with Codex's native rollout writers and manager mutations."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

from codex_mgr.core.rollouts import canonical_session_id
from codex_mgr.utils.files import FileConflictError, mutation_lock
from codex_mgr.utils.paths import get_codex_home


@contextmanager
def _native_lock(path: Path, *, create: bool) -> Iterator[None]:
    """Rust std::fs locks use flock on Unix and LockFileEx on Windows."""
    flags = os.O_RDWR | (os.O_CREAT if create else 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "r+b") as handle:
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
    session_id = canonical_session_id(session_id)
    folder = get_codex_home() / "thread-writer-locks"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (
        mutation_lock(path),
        _native_lock(folder / ".coordination.lock", create=True),
        ExitStack() as stack,
    ):
        try:
            stack.enter_context(_native_lock(folder / f"{session_id}.lock", create=False))
        except FileNotFoundError:
            pass
        yield
