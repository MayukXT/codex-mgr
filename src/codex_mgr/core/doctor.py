"""Environment diagnostic and health audit engine.

Performs non-destructive checks on the Codex installation, configuration files,
session storage, and provider connectivity to surface actionable advice.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List

from codex_mgr.config.settings import load_settings
from codex_mgr.core.models import test_provider_connectivity
from codex_mgr.core.profiles import read_config_toml
from codex_mgr.core.runner import is_codex_running
from codex_mgr.core.sessions import list_sessions
from codex_mgr.utils.paths import (
    find_codex_binary,
    get_backups_dir,
    get_cmgr_settings_path,
    get_config_toml_path,
    get_sessions_dir,
)


@dataclass
class DiagnosticResult:
    name: str
    passed: bool
    details: str
    remediation: str = ""


def run_diagnostics() -> List[DiagnosticResult]:
    """Execute all health checks and return the findings."""
    results: List[DiagnosticResult] = []

    # 1. Codex binary check
    codex_bin = find_codex_binary()
    if codex_bin and codex_bin.exists():
        results.append(DiagnosticResult(
            name="Codex CLI Binary",
            passed=True,
            details=f"Located at: {codex_bin}"
        ))
    else:
        results.append(DiagnosticResult(
            name="Codex CLI Binary",
            passed=False,
            details="Could not locate codex executable in PATH or AppData.",
            remediation="Install Codex or ensure its binary directory is added to your system PATH."
        ))

    # 2. config.toml integrity
    config_path = get_config_toml_path()
    if config_path.exists():
        data = read_config_toml()
        if data:
            mcp_count = len(data.get("mcp_servers", {}))
            active_p = data.get("model_provider", "openai")
            model = data.get("model", "default")
            results.append(DiagnosticResult(
                name="Configuration (config.toml)",
                passed=True,
                details=f"Valid TOML (Provider: {active_p}, Model: {model}, MCP Servers: {mcp_count})"
            ))
        else:
            results.append(DiagnosticResult(
                name="Configuration (config.toml)",
                passed=False,
                details="config.toml exists but could not be parsed.",
                remediation="Check for syntax errors or restore from ~/.codex/config.toml.bak."
            ))
    else:
        results.append(DiagnosticResult(
            name="Configuration (config.toml)",
            passed=False,
            details=f"config.toml not found at {config_path}",
            remediation="Run Codex once to initialize default configuration."
        ))

    # 3. cmgr.json settings
    settings_path = get_cmgr_settings_path()
    settings = load_settings()
    p_count = len(settings.providers)
    results.append(DiagnosticResult(
        name="Tool Settings (cmgr.json)",
        passed=True,
        details=f"{p_count} providers registered • Active: {settings.active_provider_id}"
    ))

    # 4. Session Storage & Rollouts
    sessions_dir = get_sessions_dir()
    all_sessions = list_sessions(limit=100)
    if sessions_dir.exists():
        results.append(DiagnosticResult(
            name="Session Storage",
            passed=True,
            details=f"Indexed {len(all_sessions)} conversation sessions in ~/.codex/sessions"
        ))
    else:
        results.append(DiagnosticResult(
            name="Session Storage",
            passed=True,
            details="Sessions directory not yet initialized."
        ))

    # 5. Process lock check
    running = is_codex_running()
    if running:
        results.append(DiagnosticResult(
            name="Active Process Lock",
            passed=True,
            details="codex.exe is currently running (Remember to close active turns before switching)."
        ))
    else:
        results.append(DiagnosticResult(
            name="Active Process Lock",
            passed=True,
            details="No active codex processes (Safe to modify sessions and switch configs)."
        ))

    # 6. Active provider connectivity test
    active_prov = settings.providers.get(settings.active_provider_id)
    if active_prov:
        healthy, msg, lat = test_provider_connectivity(active_prov)
        results.append(DiagnosticResult(
            name=f"Active Provider ({active_prov.name})",
            passed=healthy,
            details=f"{msg} ({lat}ms)" if healthy else msg,
            remediation="Verify API key and internet connectivity." if not healthy else ""
        ))

    # 7. Backup storage
    backups_dir = get_backups_dir()
    backup_count = len(list(backups_dir.glob("**/*")))
    results.append(DiagnosticResult(
        name="Backup Storage",
        passed=True,
        details=f"Initialized at ~/.codex/backups/ ({backup_count} snapshot files)"
    ))

    return results
