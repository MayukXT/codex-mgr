"""One validated activation/conversion/launch flow for CLI and TUI."""

from __future__ import annotations

from dataclasses import dataclass

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import load_settings
from codex_mgr.core.activation import apply_settings, recover_pending_activation
from codex_mgr.core.adapters.launch import AdapterLaunch
from codex_mgr.core.auth import codex_login_status
from codex_mgr.core.bridge import BridgeError, prepare_bridge
from codex_mgr.core.models import provider_headers
from codex_mgr.core.profiles import ConfigurationError, connection_id, prepare_configuration
from codex_mgr.core.runner import launch_codex
from codex_mgr.core.sessions import SessionItem
from codex_mgr.utils.files import FileConflictError, read_optional
from codex_mgr.utils.paths import find_codex_binary, get_config_toml_path


@dataclass(frozen=True)
class ResumePreview:
    connection_id: str
    session_digest: str
    changed_records: int
    encrypted_items: int


def _target(settings: CmgrSettings, provider_id: str | None, *, allow_adapter: bool = False):
    provider = settings.providers.get(provider_id or settings.active_provider_id)
    if provider is None:
        raise ConfigurationError("Choose a configured connection before resuming.")
    provider = ProviderConfig.model_validate(provider.model_dump())
    if find_codex_binary() is None:
        raise ConfigurationError("Install Codex CLI before starting a session.")
    if allow_adapter and provider.wire_api != "responses":
        if not provider.model_selected:
            raise ConfigurationError("Choose an exact model ID before starting Codex.")
    else:
        prepare_configuration(provider, read_optional(get_config_toml_path()))
    if provider.auth_type == "official_oauth":
        status = codex_login_status()
        if not status.signed_in or status.method != "chatgpt":
            raise ConfigurationError(
                "ChatGPT sign-in is not confirmed. Run codex login before resuming."
            )
    else:
        provider_headers(provider)
    return provider


def preview_resume(
    session: SessionItem, provider_id: str | None = None, *, settings: CmgrSettings | None = None
) -> ResumePreview:
    settings = settings or load_settings()
    provider = _target(settings, provider_id)
    with prepare_bridge(session.require_rollout(), connection_id(provider)) as prepared:
        return ResumePreview(
            connection_id(provider),
            prepared.expected_digest,
            prepared.changed_records,
            prepared.sanitized_count,
        )


def start_session(
    provider_id: str | None = None,
    *,
    prompt: str | None = None,
    settings: CmgrSettings | None = None,
) -> tuple[int, str]:
    """Validate the selected route and keep temporary adapters owned by the handoff."""
    try:
        if prompt is not None and "\x00" in prompt:
            return 1, "The initial prompt cannot contain a null character."
        settings = settings or load_settings()
        provider = _target(settings, provider_id, allow_adapter=True)
        if provider.wire_api == "responses":
            ok, message = apply_settings(settings, activate_provider_id=provider.id)
            if not ok:
                return 1, message
            code = launch_codex(prompt=prompt)
        else:
            # Validate controls before recovery, then verify against the recovered
            # native configuration before saving the original upstream selection.
            scope = AdapterLaunch(provider)
            ok, message = recover_pending_activation()
            if not ok:
                return 1, message
            with scope:
                previous = settings.active_provider_id
                settings.active_provider_id = provider.id
                saved = False
                try:
                    saved, message = apply_settings(settings)
                finally:
                    if not saved:
                        settings.active_provider_id = previous
                if not saved:
                    return 1, message
                code = scope.run(prompt=prompt)
        return (
            code,
            "Codex closed."
            if code == 0
            else "Codex exited unsuccessfully; the selected configuration remains available.",
        )
    except KeyboardInterrupt:
        return 130, "Session launch was cancelled."
    except (ConfigurationError, FileConflictError) as exc:
        return 1, str(exc)
    except (OSError, ValueError, RuntimeError):
        return (
            1,
            "Session was not started. Check configuration, selected credentials and pending recovery.",
        )


def resume_session(
    session: SessionItem,
    provider_id: str | None = None,
    *,
    allow_reasoning_loss: bool = False,
    expected_digest: str | None = None,
    settings: CmgrSettings | None = None,
) -> tuple[int, str]:
    """Validate before snapshots, activate before conversion, then hand off.

    Activation has its own recoverable transaction. If session publication
    subsequently fails, its current contents survive and no Codex is launched;
    the message explicitly identifies that configuration was already selected.
    """
    activated = False
    try:
        settings = settings or load_settings()
        provider = _target(settings, provider_id)
        with prepare_bridge(session.require_rollout(), connection_id(provider)) as prepared:
            if expected_digest is not None and prepared.expected_digest != expected_digest:
                raise FileConflictError(
                    "Session changed after preview. Review it again before resuming."
                )
            if prepared.sanitized_count and not allow_reasoning_loss:
                return (
                    1,
                    f"Conversion would remove {prepared.sanitized_count} encrypted items while keeping readable summaries. Review the session and use --allow-reasoning-loss to proceed.",
                )
            prepared.snapshot()
            ok, message = apply_settings(settings, activate_provider_id=provider.id)
            if not ok:
                return 1, message
            activated = True
            prepared.commit()
        code = launch_codex(session_id=session.id)
        return (
            code,
            "Codex closed."
            if code == 0
            else "Codex exited unsuccessfully. The selected configuration and recovery snapshot remain available.",
        )
    except (BridgeError, FileConflictError, ConfigurationError) as exc:
        message = str(exc)
    except (OSError, ValueError, RuntimeError):
        message = "Resume failed. Check the session, credentials, permissions and backup storage."
    if activated:
        message = "The connection was selected, but Codex was not launched. " + message
    return 1, message
