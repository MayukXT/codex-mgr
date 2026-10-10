"""Shell-free foreground handoff with truthful cancellation and error status."""

from __future__ import annotations

import subprocess

from codex_mgr.core.rollouts import canonical_session_id
from codex_mgr.utils.display import print_error
from codex_mgr.utils.paths import find_codex_binary


def launch_codex(
    session_id: str | None = None,
    prompt: str | None = None,
    extra_args: list[str] | None = None,
) -> int:
    """Launch Codex with the current terminal inherited; never interpolate a shell."""
    binary = find_codex_binary()
    if binary is None or not binary.is_file():
        print_error("Codex CLI is unavailable. Install it and add its executable to PATH.")
        return 1
    if binary.suffix.lower() in {".cmd", ".bat", ".ps1"}:
        print_error(
            "This command wrapper could not be resolved safely. Add the native Codex executable to PATH."
        )
        return 1
    command = [str(binary)]
    if session_id:
        try:
            command.extend(["resume", canonical_session_id(session_id)])
        except ValueError:
            print_error("Resume needs a complete session UUID.")
            return 1
    if extra_args:
        command.extend(extra_args)
    if prompt is not None:
        # -- keeps a leading dash in a prompt from becoming a CLI option.
        command.extend(["--", prompt])
    try:
        result = subprocess.run(command, check=False)
        return result.returncode if result.returncode >= 0 else 128 - result.returncode
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError):
        print_error("Codex could not start. Check the executable and terminal permissions.")
        return 1
