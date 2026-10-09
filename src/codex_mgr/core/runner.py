"""Codex binary execution, console handoff, and process lock guardian.

Provides process scanning to prevent WinError 32 file-lock crashes and handles
interactive terminal execution with complete stdin/stdout inheritance.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

from codex_mgr.utils.display import print_error, print_warning
from codex_mgr.utils.paths import find_codex_binary


def is_codex_running() -> bool:
    """Check if a codex.exe process is currently active on the host system."""
    try:
        if sys.platform == "win32":
            output = subprocess.check_output(
                ["tasklist", "/FI", "IMAGENAME eq codex.exe", "/NH"],
                text=True,
                stderr=subprocess.DEVNULL
            )
            return "codex.exe" in output.lower()
        else:
            res = subprocess.run(["pgrep", "-x", "codex"], stdout=subprocess.DEVNULL)
            return res.returncode == 0
    except Exception:
        return False


def launch_codex(
    session_id: Optional[str] = None,
    prompt: Optional[str] = None,
    extra_args: Optional[List[str]] = None
) -> int:
    """Launch the Codex binary with interactive console inheritance."""
    codex_bin = find_codex_binary()
    if not codex_bin or not codex_bin.exists():
        print_error("Could not locate the Codex CLI binary on your system.")
        print_warning(
            "Please ensure Codex is installed, or add its directory to your system PATH."
        )
        return 1

    cmd: List[str] = [str(codex_bin)]

    if session_id:
        cmd.extend(["resume", session_id])
        if prompt:
            cmd.append(prompt)

    if extra_args:
        cmd.extend(extra_args)

    try:
        # Full interactive terminal execution handoff
        result = subprocess.run(cmd)
        return result.returncode
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print_error(f"Failed to execute Codex: {str(exc)}")
        return 1
