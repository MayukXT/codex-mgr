"""Bounded, read-only access to Codex's documented app-server APIs."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
from collections.abc import Sequence

from codex_mgr import __version__
from codex_mgr.utils.paths import find_codex_binary


class CodexRpcError(RuntimeError):
    pass


class CodexReader:
    """One initialized stdio connection; no sessions, turns or writes are allowed."""

    ALLOWED = frozenset({"model/list", "account/read", "config/read", "thread/list", "thread/read"})

    def __init__(self, *, overrides: Sequence[str] = (), command=None, timeout=10):
        binary = find_codex_binary() if command is None else None
        if command is None and binary is None:
            raise CodexRpcError("Codex CLI is unavailable.")
        args = list(command) if command is not None else [str(binary)]
        for override in overrides:
            args.extend(["-c", override])
        if command is None:
            args.append("app-server")
        self.timeout = timeout
        self.sequence = 0
        self.messages = queue.Queue(maxsize=128)
        self.process = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()
        try:
            self._request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "codex_manager",
                        "title": "Codex Manager",
                        "version": __version__,
                    }
                },
            )
            self._send({"method": "initialized", "params": {}})
        except (CodexRpcError, OSError):
            self.close()
            raise

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(1_048_577)
                if not line:
                    break
                if len(line) > 1_048_576:
                    break
                self.messages.put(json.loads(line), timeout=self.timeout)
        except (ValueError, OSError, queue.Full):
            pass
        finally:
            try:
                self.messages.put(None, timeout=self.timeout)
            except queue.Full:
                pass

    def _send(self, message):
        try:
            self.process.stdin.write(json.dumps(message) + "\n")
            self.process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise CodexRpcError("Codex app-server connection closed.") from exc

    def _request(self, method, params):
        self.sequence += 1
        identity = self.sequence
        self._send({"id": identity, "method": method, "params": params})
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                message = self.messages.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty as exc:
                raise CodexRpcError("Codex app-server request timed out.") from exc
            if not isinstance(message, dict):
                raise CodexRpcError("Codex app-server returned an invalid response.")
            if message.get("id") != identity or "method" in message:
                if "id" in message and "method" in message:
                    self._send(
                        {
                            "id": message["id"],
                            "error": {
                                "code": -32601,
                                "message": "Read-only client",
                            },
                        }
                    )
                continue
            if "error" in message:
                raise CodexRpcError("Codex could not complete this read request.")
            result = message.get("result")
            if not isinstance(result, dict):
                raise CodexRpcError("Codex returned an invalid read result.")
            return result

    def request(self, method, params=None):
        if method not in self.ALLOWED:
            raise CodexRpcError("This client permits documented read methods only.")
        return self._request(method, params or {})

    def pages(self, method, params=None):
        values = dict(params or {})
        seen = set()
        while True:
            result = self.request(method, values)
            data = result.get("data")
            if not isinstance(data, list):
                raise CodexRpcError("Codex returned an invalid catalog page.")
            yield from data
            cursor = result.get("nextCursor")
            if not cursor:
                return
            if not isinstance(cursor, str) or cursor in seen:
                raise CodexRpcError("Codex returned a repeated pagination cursor.")
            seen.add(cursor)
            values["cursor"] = cursor

    def close(self):
        if self.process.stdin and not self.process.stdin.closed:
            try:
                self.process.stdin.close()
            except OSError:
                pass
        if self.process.poll() is None:
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # A Windows command shim can own a native child holding our pipe.
                import os

                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=3,
                        check=False,
                    )
                else:
                    self.process.terminate()
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=2)
        self.reader.join(timeout=1)
        # Never block closing a buffered pipe while its reader owns the lock.
        if not self.reader.is_alive() and self.process.stdout:
            self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
