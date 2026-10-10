"""Read-only diagnostics with explicit limits on what each check establishes."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from codex_mgr.config.settings import load_settings
from codex_mgr.core.auth import CredentialError, codex_login_status
from codex_mgr.core.models import provider_headers, test_provider_connectivity
from codex_mgr.core.profiles import effective_configuration, prepare_configuration, read_config_toml
from codex_mgr.core.sessions import discover_sessions
from codex_mgr.utils.files import read_optional
from codex_mgr.utils.paths import find_codex_binary, get_codex_home, get_config_toml_path

DiagnosticStatus = Literal["passed", "failed", "warning", "not_checked"]


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    status: DiagnosticStatus
    details: str
    remediation: str = ""

    @property
    def passed(self) -> bool:
        return self.status == "passed"


def _storage_results() -> list[DiagnosticResult]:
    results = []
    try:
        catalog = discover_sessions(include_archived=True)
        duplicates = sum(bool(item.duplicate_paths) for item in catalog.sessions)
        missing = sum(item.rollout_path is None for item in catalog.sessions)
        skipped = catalog.skipped_rollouts + catalog.skipped_index_records
        incomplete = bool(duplicates or missing or skipped or catalog.unreadable_sources)
        results.append(
            DiagnosticResult(
                "Session catalog",
                "warning" if incomplete else "passed",
                f"{len(catalog.sessions)} sessions; {duplicates} duplicate UUIDs; "
                f"{missing} missing rollouts; {skipped} invalid entries; "
                f"{catalog.unreadable_sources} unreadable sources.",
                "Resolve duplicate or unreadable history before resuming it." if incomplete else "",
            )
        )
    except (OSError, ValueError):
        results.append(
            DiagnosticResult(
                "Session catalog",
                "failed",
                "Could not read the local session catalog.",
                "Check session storage permissions. Existing history was not changed.",
            )
        )
    # Do not call get_backups_dir(): diagnostics must not create directories.
    home = get_codex_home()
    folder = home / "backups"
    try:
        root = folder.resolve()
        root.relative_to(home)
        count = 0
        for pattern in (
            "config/config_*.toml",
            "config/kept_*.toml",
            "settings/cmgr_*.json",
            "settings/kept_*.json",
            "sessions/*/*.rollout.jsonl",
        ):
            for path in folder.glob(pattern):
                path.resolve().relative_to(root)
                count += path.is_file()
        results.append(
            DiagnosticResult(
                "Backup inventory",
                "passed",
                f"{count} snapshot files; integrity was not checked.",
            )
        )
        pending = 0
        for path in folder.glob("transactions/*/journal.json"):
            path.resolve().relative_to(root)
            record = json.loads(path.read_bytes())
            state = record.get("state") if isinstance(record, dict) else None
            if not isinstance(state, str) or state not in {
                "prepared",
                "config_applied",
                "validated",
                "committed",
                "rolled_back",
                "accepted_external",
            }:
                raise ValueError
            pending += state not in {"committed", "rolled_back", "accepted_external"}
        results.append(
            DiagnosticResult(
                "Activation recovery",
                "failed" if pending else "passed",
                f"{pending} pending transactions." if pending else "No pending transactions found.",
                "Run cmgr recover before applying another connection." if pending else "",
            )
        )
    except (OSError, ValueError):
        results.append(
            DiagnosticResult(
                "Backup inventory / recovery",
                "failed",
                "Backup or recovery records are unreadable.",
                "Inspect backup storage and recovery records before retrying changes.",
            )
        )
    results.append(
        DiagnosticResult(
            "Session writer ownership",
            "not_checked",
            "Writer locks are checked for the selected session when a mutation starts.",
        )
    )
    return results


def _connection_results(provider, *, network: bool) -> list[DiagnosticResult]:
    results = []
    if not provider.model_selected:
        results.append(
            DiagnosticResult(
                "Connection application",
                "warning",
                "No exact model ID has been selected.",
                "Choose an available model or enter an exact model ID before activation.",
            )
        )
    else:
        try:
            prepare_configuration(provider, read_optional(get_config_toml_path()))
            results.append(
                DiagnosticResult(
                    "Connection application",
                    "passed",
                    "Local application rules accepted this connection.",
                )
            )
        except (OSError, ValueError, TypeError):
            results.append(
                DiagnosticResult(
                    "Connection application",
                    "failed",
                    "This connection cannot currently be applied.",
                    "Check protocol support, model controls, credentials and enforced login settings.",
                )
            )
    if provider.auth_type == "official_oauth":
        status = codex_login_status()
        credentials_ok = status.signed_in and status.method == "chatgpt"
        results.append(
            DiagnosticResult(
                "Selected authentication",
                "passed"
                if credentials_ok
                else "warning"
                if status.method == "unavailable"
                else "failed",
                "Codex reports ChatGPT sign-in; inference access was not tested."
                if credentials_ok
                else "ChatGPT sign-in is not confirmed for this connection.",
                "Run codex login, then retry the check." if not credentials_ok else "",
            )
        )
    else:
        try:
            provider_headers(provider)
            credentials_ok = provider.auth_type == "none" or bool(
                provider.auth_token or provider.http_headers or provider.env_http_headers
            )
        except (CredentialError, ValueError):
            credentials_ok = False
        results.append(
            DiagnosticResult(
                "Selected authentication",
                "passed" if credentials_ok else "failed",
                "No authentication requested."
                if credentials_ok and provider.auth_type == "none"
                else "Selected credential references resolve; upstream acceptance was not tested."
                if credentials_ok
                else "The selected credential is missing or invalid.",
                "Set this connection's credential; other provider keys are not a fallback."
                if not credentials_ok
                else "",
            )
        )
    if network and credentials_ok and provider.auth_type != "official_oauth":
        try:
            ok, _message, elapsed = test_provider_connectivity(provider)
            results.append(
                DiagnosticResult(
                    "Provider catalog",
                    "passed" if ok else "failed",
                    f"Selected catalog received ({elapsed} ms); inference was not tested."
                    if ok
                    else "Selected catalog request failed.",
                    "Check this connection's endpoint, credentials and network." if not ok else "",
                )
            )
        except (OSError, ValueError, RuntimeError):
            results.append(
                DiagnosticResult(
                    "Provider catalog",
                    "failed",
                    "Selected catalog request could not complete.",
                    "Check this connection's endpoint, credentials and network.",
                )
            )
    else:
        results.append(
            DiagnosticResult(
                "Provider catalog",
                "not_checked",
                "No catalog request was made.",
                "Use cmgr doctor --network to check API catalog access."
                if not network and provider.auth_type != "official_oauth"
                else "",
            )
        )
    return results


def run_diagnostics(*, network: bool = False) -> list[DiagnosticResult]:
    """Check local state; network=True additionally requests an API catalog.

    This never sends an inference request, changes settings or restores history.
    ChatGPT sign-in status is read through Codex rather than cached token metadata.
    """
    results = []
    try:
        binary = find_codex_binary()
        results.append(
            DiagnosticResult(
                "Codex CLI",
                "passed" if binary is not None else "failed",
                "Executable found." if binary is not None else "Executable not found.",
                "Install Codex CLI and add it to PATH." if binary is None else "",
            )
        )
    except OSError:
        results.append(
            DiagnosticResult(
                "Codex CLI",
                "failed",
                "Executable discovery could not complete.",
                "Check PATH and executable permissions.",
            )
        )
    config_ok = False
    try:
        data = read_config_toml()
        effective_configuration(data)
        config_ok = True
        results.append(
            DiagnosticResult(
                "Local configuration",
                "passed",
                "config.toml parsed; selected profile exists."
                if get_config_toml_path().exists()
                else "No local config.toml; Codex defaults apply.",
            )
        )
    except (OSError, ValueError, TypeError):
        results.append(
            DiagnosticResult(
                "Local configuration",
                "failed",
                "Local configuration could not be read or parsed.",
                "Repair config.toml or restore a verified configuration revision.",
            )
        )
    results.append(
        DiagnosticResult(
            "Effective Codex configuration",
            "not_checked",
            "Managed settings and invocation overrides were not checked by this local-file check.",
        )
    )
    settings = None
    if config_ok:
        try:
            settings = load_settings()
            results.append(
                DiagnosticResult(
                    "Manager settings",
                    "passed",
                    f"{len(settings.providers)} saved connections loaded.",
                )
            )
        except (OSError, ValueError, TypeError):
            results.append(
                DiagnosticResult(
                    "Manager settings",
                    "failed",
                    "Manager settings or selected connection are invalid.",
                    "Repair cmgr.json and the selected connection definition before applying changes.",
                )
            )
    else:
        results.append(
            DiagnosticResult(
                "Manager settings",
                "not_checked",
                "Loading the selected connection needs valid configuration.",
            )
        )
    provider = settings.providers.get(settings.active_provider_id) if settings else None
    if settings is not None and provider is None:
        results.append(
            DiagnosticResult(
                "Selected connection",
                "failed",
                "The saved active connection does not exist.",
                "Select an existing connection or add its definition.",
            )
        )
    if provider is not None:
        results.extend(_connection_results(provider, network=network))
    results.extend(_storage_results())
    results.append(
        DiagnosticResult(
            "Coding workflow",
            "not_checked",
            "Inference, tool execution and end-to-end resumption were not tested.",
        )
    )
    return results
