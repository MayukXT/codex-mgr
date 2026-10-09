"""Version checking, release discovery, and self-update management.

Enables non-technical users to inspect their installed version, check for updates
on PyPI, and upgrade codex-mgr with a single command or TUI action.
"""

from __future__ import annotations

import subprocess
import sys
from typing import Optional, Tuple

import httpx

try:
    from packaging.version import parse as parse_version
except ImportError:
    def parse_version(v: str):
        # Fallback numeric tuple parser
        nums = []
        for part in v.replace("-", ".").split("."):
            nums.append(int(part) if part.isdigit() else 0)
        return tuple(nums)

from codex_mgr import __version__


def get_current_version() -> str:
    """Return the installed version string."""
    return __version__


def check_for_updates() -> Tuple[bool, Optional[str], str]:
    """Query PyPI for newer releases of codex-mgr.

    Returns:
        (has_update, latest_version_string, message)
    """
    url = "https://pypi.org/pypi/codex-mgr/json"
    try:
        with httpx.Client(timeout=3.5) as client:
            resp = client.get(url)
            if resp.status_code == 200:
                data = resp.json()
                latest_v = data.get("info", {}).get("version", __version__)
                try:
                    if parse_version(latest_v) > parse_version(__version__):
                        return True, latest_v, f"A new version of codex-mgr is available: v{latest_v} (Current: v{__version__})"
                except Exception:
                    pass
                return False, latest_v, f"codex-mgr is up to date (v{__version__})"
            elif resp.status_code == 404:
                return False, __version__, f"codex-mgr is running on version v{__version__} (Development/Local build)"
            return False, None, f"Could not check for updates (HTTP {resp.status_code})"
    except Exception as exc:
        return False, None, f"Could not check for updates: {str(exc)}"


def perform_self_update() -> Tuple[bool, str]:
    """Execute 'pip install --upgrade codex-mgr' to upgrade the installation."""
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "codex-mgr"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=60.0)
        if res.returncode == 0:
            return True, "codex-mgr successfully updated to the latest version!"
        return False, f"Update failed: {res.stderr.strip() or res.stdout.strip()}"
    except Exception as exc:
        return False, f"Failed to execute update: {str(exc)}"
