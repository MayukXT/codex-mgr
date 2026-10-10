"""Ownership and bounded cleanup for stdio background helpers."""

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Sequence


class BackgroundProcessError(RuntimeError):
    pass


# The supervisor cannot spawn the target until its owner releases the gate.
# Byte reads avoid consuming buffered RPC data that belongs to the target.
# Keeping this leader alive pins the POSIX process-group ID until cleanup.
_SUPERVISOR = """
import json,os,signal,subprocess,time
if os.name!='nt':
    # A caught handler resets on exec, so the target retains normal SIGTERM.
    signal.signal(signal.SIGTERM,lambda *_: None)
data=bytearray()
try:
    while len(data)<1048576:
        value=os.read(0,1)
        if not value or value==b'\\n': break
        data.extend(value)
    if not value or value!=b'\\n': raise ValueError()
    command=json.loads(data.decode('utf-8'))
    child=subprocess.Popen(command,stdin=0,stdout=1,stderr=subprocess.DEVNULL,
                           creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    child.wait()
except Exception:
    pass
finally:
    for fd in (0,1):
        try: os.close(fd)
        except OSError: pass
    try: os.write(2,b'x')
    except OSError: pass
while True: time.sleep(3600)
"""


class _WindowsJob:
    """Unnamed noninheritable job; all normal descendants die on last close."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                (name, ctypes.c_ulonglong)
                for name in (
                    "ReadOperationCount",
                    "WriteOperationCount",
                    "OtherOperationCount",
                    "ReadTransferCount",
                    "WriteTransferCount",
                    "OtherTransferCount",
                )
            ]

        class ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", BasicLimits),
                ("IoInfo", IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        self._api = ctypes.WinDLL("kernel32", use_last_error=True)
        self._api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self._api.CreateJobObjectW.restype = wintypes.HANDLE
        self._api.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self._api.SetInformationJobObject.restype = wintypes.BOOL
        self._api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._api.AssignProcessToJobObject.restype = wintypes.BOOL
        self._api.CloseHandle.argtypes = [wintypes.HANDLE]
        self._api.CloseHandle.restype = wintypes.BOOL
        self._handle = self._api.CreateJobObjectW(None, None)
        if not self._handle:
            raise BackgroundProcessError("Background process ownership is unavailable.")
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        if not self._api.SetInformationJobObject(
            self._handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            self.close()
            raise BackgroundProcessError("Background process ownership is unavailable.")

    def assign(self, process):
        if not self._api.AssignProcessToJobObject(self._handle, int(process._handle)):
            raise BackgroundProcessError("Background process ownership is unavailable.")

    def close(self):
        if self._handle is not None:
            if not self._api.CloseHandle(self._handle):
                raise BackgroundProcessError("Background process cleanup failed.")
            self._handle = None


class OwnedBackgroundProcess:
    """Own a hidden stdio process tree without discovering unrelated processes."""

    def __init__(self, command: Sequence[str], *, env=None, cwd=None, shutdown_timeout=2):
        if (
            not isinstance(shutdown_timeout, (int, float))
            or not math.isfinite(shutdown_timeout)
            or not 0 <= shutdown_timeout <= 30
        ):
            raise BackgroundProcessError("Invalid background shutdown timeout.")
        args = [os.fspath(value) for value in command]
        if not args or any(not isinstance(value, str) or "\0" in value for value in args):
            raise BackgroundProcessError("Invalid background command.")
        gate = json.dumps(args, ensure_ascii=True) + "\n"
        if len(gate) > 1_048_576:
            raise BackgroundProcessError("Background command exceeds the size limit.")
        self._process = None
        self._job = None
        self._watcher = None
        self._closed = False
        self._done = threading.Event()
        self._shutdown_timeout = shutdown_timeout
        self._windows = os.name == "nt"
        try:
            if self._windows:
                self._job = _WindowsJob()
            self._process = subprocess.Popen(
                [sys.executable, "-I", "-S", "-c", _SUPERVISOR],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=env,
                cwd=cwd,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                start_new_session=not self._windows,
            )
            if self._job:
                self._job.assign(self._process)
            self._watcher = threading.Thread(target=self._watch_completion, daemon=True)
            self._watcher.start()
            self._process.stdin.write(gate)
            self._process.stdin.flush()
        except BaseException:
            # The native command is still gated if ownership assignment failed.
            self.close(startup_failure=True)
            raise

    @property
    def process(self):
        return self._process

    def _watch_completion(self):
        try:
            self._process.stderr.read(1)
        except (OSError, ValueError):
            pass
        finally:
            self._done.set()

    def close(self, *, startup_failure=False):
        if self._closed:
            return
        process = self._process
        if process is None:
            if self._job:
                self._job.close()
            self._closed = True
            return
        if process.stdin and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        if not startup_failure:
            self._done.wait(timeout=self._shutdown_timeout)
        if self._windows:
            if startup_failure:
                # Assign may have failed; terminate only our still-gated child.
                process.kill()
            if self._job:
                self._job.close()
        else:
            # Never reap the pinned supervisor before the final group signal.
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(process.pid, sig)
                except ProcessLookupError:
                    break
                if sig == signal.SIGTERM:
                    time.sleep(0.05)
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired as exc:
            raise BackgroundProcessError("Background process cleanup timed out.") from exc
        self._closed = True
        if self._watcher and self._watcher.ident is not None:
            self._watcher.join(timeout=1)
        if not self._watcher or not self._watcher.is_alive():
            process.stderr.close()
        if startup_failure:
            process.stdout.close()
