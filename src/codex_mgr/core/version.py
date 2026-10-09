"""Release checks that distinguish unavailable metadata from a current install."""

from __future__ import annotations

import subprocess
import sys

import httpx
from packaging.version import InvalidVersion, Version

from codex_mgr import __version__

_PACKAGE = "cmgr-cli"
_INDEX_URL = f"https://pypi.org/pypi/{_PACKAGE}/json"


def get_current_version() -> str:
    return __version__


def check_for_updates() -> tuple[bool, str | None, str]:
    """Return (update_available, verified_latest_or_none, safe_message)."""
    try:
        with httpx.Client(timeout=3.5, follow_redirects=False) as client:
            response = client.get(_INDEX_URL)
        if response.status_code != 200:
            return False, None, f"Update check unavailable (HTTP {response.status_code})."
        data = response.json()
        latest = data.get("info", {}).get("version") if isinstance(data, dict) else None
        if not isinstance(latest, str) or len(latest) > 128:
            raise ValueError
        published, installed = Version(latest), Version(__version__)
        latest = str(published)
        if published.is_prerelease or published.is_devrelease or published.local is not None:
            return False, None, "A stable public release could not be verified."
        if data.get("info", {}).get("yanked") is True:
            return False, None, "The published release is withdrawn; no update was offered."
        if published > installed:
            return True, latest, f"cmgr-cli v{latest} is available (installed: v{__version__})."
        if published < installed:
            return False, latest, f"Installed v{__version__} is newer than the published v{latest}."
        return False, latest, f"cmgr-cli is up to date (v{__version__})."
    except (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError):
        return False, None, "Update check failed; release metadata could not be verified."


def perform_self_update(target_version: str) -> tuple[bool, str]:
    """Install the checked release into this interpreter, then verify metadata.

    pip output is withheld because index configuration and errors may contain secrets.
    User-managed pip configuration still applies; failed verification is not success.
    """
    try:
        target = Version(target_version)
        if (
            target <= Version(__version__)
            or target.is_prerelease
            or target.is_devrelease
            or target.local
        ):
            raise ValueError
    except (InvalidVersion, ValueError, TypeError):
        return False, "Choose a verified stable release newer than the installed version."
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "--upgrade",
                "--no-input",
                f"{_PACKAGE}=={target}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=45,
            check=False,
        )
        if result.returncode != 0:
            return (
                False,
                "pip could not install the release. Use your environment's package manager to update cmgr-cli.",
            )
        installed = subprocess.run(
            [
                sys.executable,
                "-c",
                "import importlib.metadata; print(importlib.metadata.version('cmgr-cli'))",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if installed.returncode != 0 or Version(installed.stdout.strip()) != target:
            return (
                False,
                "pip finished, but this interpreter's installed version could not be verified.",
            )
        return True, f"Installed cmgr-cli v{target}. Restart cmgr and check cmgr --version."
    except (OSError, subprocess.SubprocessError, ValueError, TypeError):
        return (
            False,
            "Update did not complete or could not be verified. Check the installed version before retrying.",
        )
