"""Shared config/settings activation with rollback and crash recovery."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from codex_mgr.config.schema import CmgrSettings
from codex_mgr.config.settings import (
    commit_settings,
    load_settings,
    parse_settings,
    save_settings,
    serialize_settings,
)
from codex_mgr.core.native_config import (
    NativeConfigurationReadError,
    validate_activation_configuration,
    verify_after_activation,
    verify_before_activation,
)
from codex_mgr.core.profiles import (
    ConfigurationError,
    parse_configuration,
    prepare_configuration,
    snapshot_configuration,
)
from codex_mgr.utils.files import (
    FileConflictError,
    atomic_write,
    create_snapshot,
    mutation_lock,
    read_optional,
)
from codex_mgr.utils.paths import get_cmgr_settings_path, get_codex_home, get_config_toml_path

_TERMINAL = frozenset({"committed", "rolled_back", "accepted_external"})
_STATES = _TERMINAL | {"prepared", "config_applied", "validated"}
_UNSET = object()


@dataclass(frozen=True)
class PendingActivation:
    id: str
    state: str
    config_status: str
    settings_status: str
    config_digest: str | None = field(repr=False)
    settings_digest: str | None = field(repr=False)

    @property
    def expected(self):
        return self.config_digest, self.settings_digest


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _journal(path: Path, record: dict) -> None:
    _contained(path)
    atomic_write(path, json.dumps(record).encode("utf-8"))


def _contained(path: Path) -> Path:
    """Resolve before reading or writing recovery data, including linked preimages."""
    root = get_codex_home()
    backups = root / "backups"
    backups.resolve().relative_to(root)
    path.resolve().relative_to(backups.resolve())
    return path


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Invalid activation recovery record.")
        result[key] = value
    return result


def _read_record(path: Path) -> dict:
    _contained(path)
    if path.stat().st_size > 16_384:
        raise ValueError("Activation recovery record exceeds the supported size.")
    record = json.loads(path.read_bytes(), object_pairs_hook=_unique_pairs)
    if not isinstance(record, dict) or not isinstance(record.get("state"), str):
        raise ConfigurationError("Invalid activation recovery record.")
    if record["state"] not in _STATES:
        raise ValueError("Unknown activation recovery state.")
    for name in ("config", "settings"):
        for prefix in (
            ("before", "after", "accepted")
            if record["state"] == "accepted_external"
            else ("before", "after")
        ):
            key = f"{prefix}_{name}"
            if key not in record or (
                record[key] is not None
                and (
                    not isinstance(record[key], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", record[key])
                )
            ):
                raise ValueError("Invalid activation recovery digest.")
    return record


def _originals(path: Path, record: dict) -> dict[str, bytes | None]:
    originals = {}
    for name in ("config", "settings"):
        digest = record[f"before_{name}"]
        data = (
            _contained(path.parent / f"before-{name}.bin").read_bytes()
            if digest is not None
            else None
        )
        if _digest(data) != digest:
            raise ValueError("Recovery snapshot integrity check failed.")
        originals[name] = data
    return originals


def _status(digest, record, name):
    before, after = record[f"before_{name}"], record[f"after_{name}"]
    if digest == before == after:
        return "unchanged"
    if digest == before:
        return "original"
    if digest == after:
        return "requested"
    return "external"


def list_pending_activations() -> list[PendingActivation]:
    """Read only safe metadata; no directories, snapshots or credential output."""
    folder = _contained(get_codex_home() / "backups" / "transactions")
    result = []
    for path in sorted(folder.glob("*/journal.json")):
        record = _read_record(path)
        if record["state"] in _TERMINAL:
            continue
        if not re.fullmatch(r"[a-f0-9]{32}", path.parent.name):
            raise ValueError("Pending activation has an invalid transaction ID.")
        _originals(path, record)
        config, settings = (
            _digest(read_optional(get_config_toml_path())),
            _digest(read_optional(get_cmgr_settings_path())),
        )
        result.append(
            PendingActivation(
                path.parent.name,
                record["state"],
                _status(config, record, "config"),
                _status(settings, record, "settings"),
                config,
                settings,
            )
        )
    return result


def accept_external_activation(
    transaction_id: str, *, expected: tuple[str | None, str | None]
) -> tuple[bool, str]:
    """Explicitly keep reviewed current files, snapshot them and settle one journal."""
    if not isinstance(transaction_id, str) or not re.fullmatch(r"[a-f0-9]{32}", transaction_id):
        return False, "Choose an exact pending transaction ID from cmgr recover --list."
    try:
        path = _contained(
            get_codex_home() / "backups" / "transactions" / transaction_id / "journal.json"
        )
        with mutation_lock(get_config_toml_path()), mutation_lock(get_cmgr_settings_path()):
            record = _read_record(path)
            if record["state"] in _TERMINAL:
                return False, "This activation transaction is already settled."
            _originals(path, record)
            pairs = [
                ("config", get_config_toml_path(), ".toml"),
                ("settings", get_cmgr_settings_path(), ".json"),
            ]
            current = {name: read_optional(target) for name, target, _ in pairs}
            digests = tuple(_digest(current[name]) for name, _, _ in pairs)
            if digests != expected:
                raise FileConflictError(
                    "Files changed after review; inspect the transaction again."
                )
            for name, target, suffix in pairs:
                if current[name] is not None:
                    folder = _contained(get_codex_home() / "backups" / name)
                    create_snapshot(target, folder, prefix="kept_", suffix=suffix)
            if any(read_optional(target) != current[name] for name, target, _ in pairs):
                raise FileConflictError("Files changed while snapshotting; they were preserved.")
            record["state"] = "accepted_external"
            record["accepted_config"], record["accepted_settings"] = digests
            _journal(path, record)
        return (
            True,
            "Current files kept and snapshotted. Transaction settled; reload settings before activation.",
        )
    except (OSError, ValueError, RuntimeError):
        return (
            False,
            "Transaction was not settled. Check recovery integrity, file changes and backup storage.",
        )


def _restore(path: Path, original: bytes | None, expected: bytes | None) -> None:
    if read_optional(path) != expected:
        raise FileConflictError("Recovery found an external edit; it was preserved.")
    if original is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, original, expected=expected)


def _recover_journals() -> None:
    folder = _contained(get_codex_home() / "backups" / "transactions")
    for path in sorted(folder.glob("*/journal.json")):
        try:
            record = _read_record(path)
            if record["state"] in _TERMINAL:
                continue
            pairs = [
                ("config", get_config_toml_path()),
                ("settings", get_cmgr_settings_path()),
            ]
            current = {name: read_optional(target) for name, target in pairs}
            if record["state"] == "validated" and all(
                _digest(current[name]) == record[f"after_{name}"] for name, _ in pairs
            ):
                record["state"] = "committed"
                _journal(path, record)
                continue
            originals = _originals(path, record)
            for name, _ in pairs:
                old_hash = record[f"before_{name}"]
                if _digest(current[name]) not in (old_hash, record[f"after_{name}"]):
                    raise FileConflictError(
                        "Recovery found an external edit; inspect the pending transaction before retrying."
                    )
            for name, target in pairs:
                if current[name] != originals[name]:
                    _restore(target, originals[name], current[name])
            record["state"] = "rolled_back"
            _journal(path, record)
        except (KeyError, json.JSONDecodeError) as exc:
            raise ValueError(
                "Invalid activation recovery record; inspect it before retrying."
            ) from exc


def recover_pending_activation() -> tuple[bool, str]:
    try:
        with mutation_lock(get_config_toml_path()), mutation_lock(get_cmgr_settings_path()):
            _recover_journals()
        return True, "Activation recovery checked."
    except (OSError, ValueError, RuntimeError):
        return False, "Pending recovery could not be completed. Existing files were preserved."


def apply_settings(
    settings: CmgrSettings, *, activate_provider_id: str | None = None
) -> tuple[bool, str]:
    """Use this for saved provider/model edits in both terminal and CLI flows."""
    if activate_provider_id is None:
        try:
            save_settings(settings)
            return True, "Settings saved."
        except (OSError, ValueError, RuntimeError):
            return False, "Settings were not saved. Reload or check permissions and backups."
    return _apply_transaction(settings, activate_provider_id=activate_provider_id)


def apply_configuration(
    settings: CmgrSettings,
    configuration: bytes,
    *,
    expected_config: bytes | None,
    repair_current: bool = False,
) -> tuple[bool, str]:
    """Restore a reviewed candidate through the native-verified recovery transaction."""
    return _apply_transaction(
        settings,
        activate_provider_id=settings.active_provider_id,
        configuration=configuration,
        expected_config=expected_config,
        repair_current=repair_current,
    )


def _apply_transaction(
    settings: CmgrSettings,
    *,
    activate_provider_id: str,
    configuration: bytes | None = None,
    expected_config: bytes | None | object = _UNSET,
    repair_current: bool = False,
) -> tuple[bool, str]:
    target = settings.providers.get(activate_provider_id)
    if target is None:
        return False, "Unknown provider. Add it before activating it."
    old_active = settings.active_provider_id
    config_path, settings_path = get_config_toml_path(), get_cmgr_settings_path()
    journal_path = None
    record = None
    config_before = settings_before = config_after = settings_after = None
    success = (
        "Configuration and settings restored."
        if configuration is not None
        else f"Selected {target.name}: {target.default_model}"
    )
    try:
        with mutation_lock(config_path), mutation_lock(settings_path):
            _recover_journals()
            config_before = read_optional(config_path)
            settings_before = read_optional(settings_path)
            if expected_config is not _UNSET and config_before != expected_config:
                raise FileConflictError("Configuration changed after review; inspect it again.")
            if settings._loaded and settings_before != settings._source_bytes:
                raise FileConflictError("Settings changed; reload before activating.")
            if configuration is not None:
                _contained(get_codex_home() / "backups" / "config")
                _contained(get_codex_home() / "backups" / "settings")
                for path in (config_path, settings_path):
                    path.with_suffix(path.suffix + ".bak").resolve().relative_to(get_codex_home())
            if settings_before is not None and not repair_current:
                parse_settings(settings_before)
            config_after = (
                configuration
                if configuration is not None
                else prepare_configuration(target, config_before)
            )
            validate_activation_configuration(config_after)
            malformed_config = False
            try:
                parse_configuration(config_before)
            except ConfigurationError:
                if not repair_current or configuration is None:
                    raise
                malformed_config = True
            if not malformed_config:
                try:
                    verify_before_activation(config_after)
                except NativeConfigurationReadError:
                    if not repair_current or configuration is None:
                        raise
                    # Explicit repair can replace a native-unreadable current file.
                    # A policy/layer rejection is different and cannot bypass preflight.
            settings.active_provider_id = activate_provider_id
            settings_after = serialize_settings(settings)
            transaction = _contained(
                get_codex_home() / "backups" / "transactions" / uuid.uuid4().hex
            )
            record = {
                "state": "prepared",
                "before_config": _digest(config_before),
                "before_settings": _digest(settings_before),
                "after_config": _digest(config_after),
                "after_settings": _digest(settings_after),
            }
            for name, data in (("config", config_before), ("settings", settings_before)):
                if data is not None:
                    atomic_write(
                        _contained(transaction / f"before-{name}.bin"), data, expected=None
                    )
            journal_path = transaction / "journal.json"
            _journal(journal_path, record)
            snapshot_configuration()
            if configuration is not None and settings_before is not None:
                create_snapshot(
                    settings_path,
                    _contained(get_codex_home() / "backups" / "settings"),
                    prefix="cmgr_",
                    suffix=".json",
                )
                atomic_write(settings_path.with_suffix(".json.bak"), settings_before)
            try:
                atomic_write(config_path, config_after, expected=config_before)
                record["state"] = "config_applied"
                _journal(journal_path, record)
                verify_after_activation(config_after)
                if read_optional(config_path) != config_after:
                    raise FileConflictError(
                        "Configuration changed during verification; it was preserved."
                    )
                record["state"] = "validated"
                _journal(journal_path, record)
                if configuration is None:
                    commit_settings(settings, expected=settings_before)
                else:
                    commit_settings(
                        settings,
                        expected=settings_before,
                        create_backup=False,
                        allow_invalid_current=repair_current,
                    )
            except (OSError, ValueError, RuntimeError):
                # Recovery is conditional: never roll back an unrelated external edit.
                _recover_journals()
                raise
            record["state"] = "committed"
            _journal(journal_path, record)
        return True, success
    except ConfigurationError as exc:
        message = str(exc)
    except FileConflictError as exc:
        message = str(exc)
    except (OSError, ValueError, RuntimeError):
        message = "Activation failed. Check permissions and pending recovery before retrying."
    if (
        journal_path is not None
        and record is not None
        and record["state"] in {"validated", "committed"}
        and read_optional(config_path) == config_after
        and read_optional(settings_path) == settings_after
    ):
        return (
            True,
            f"{success} Check the pending recovery marker.",
        )
    settings.active_provider_id = old_active
    settings._source_bytes = (
        settings_before if settings_before is not None else settings._source_bytes
    )
    return False, message


def activate_provider(provider_id: str) -> tuple[bool, str]:
    try:
        return apply_settings(load_settings(), activate_provider_id=provider_id)
    except (OSError, ValueError, RuntimeError):
        return (
            False,
            "Could not read current configuration or settings. Repair them before activating.",
        )
