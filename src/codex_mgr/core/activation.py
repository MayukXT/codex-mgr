"""Shared config/settings activation with rollback and crash recovery."""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path

from codex_mgr.config.schema import CmgrSettings
from codex_mgr.config.settings import (
    commit_settings,
    load_settings,
    save_settings,
    serialize_settings,
)
from codex_mgr.core.profiles import (
    ConfigurationError,
    prepare_configuration,
    snapshot_configuration,
)
from codex_mgr.utils.files import FileConflictError, atomic_write, mutation_lock, read_optional
from codex_mgr.utils.paths import get_backups_dir, get_cmgr_settings_path, get_config_toml_path


def _digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _journal(path: Path, record: dict) -> None:
    atomic_write(path, json.dumps(record).encode("utf-8"))


def _restore(path: Path, original: bytes | None, expected: bytes | None) -> None:
    if read_optional(path) != expected:
        raise FileConflictError("Recovery found an external edit; it was preserved.")
    if original is None:
        path.unlink(missing_ok=True)
    else:
        atomic_write(path, original, expected=expected)


def _recover_journals() -> None:
    folder = get_backups_dir() / "transactions"
    for path in sorted(folder.glob("*/journal.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["state"] in ("committed", "rolled_back"):
                continue
            pairs = [
                ("config", get_config_toml_path()),
                ("settings", get_cmgr_settings_path()),
            ]
            current = {name: read_optional(target) for name, target in pairs}
            if all(_digest(current[name]) == record[f"after_{name}"] for name, _ in pairs):
                record["state"] = "committed"
                _journal(path, record)
                continue
            originals = {}
            for name, _ in pairs:
                old_hash = record[f"before_{name}"]
                original = (
                    (path.parent / f"before-{name}.bin").read_bytes()
                    if old_hash is not None
                    else None
                )
                if _digest(original) != old_hash:
                    raise ValueError("Recovery snapshot integrity check failed.")
                if _digest(current[name]) not in (old_hash, record[f"after_{name}"]):
                    raise FileConflictError(
                        "Recovery found an external edit; inspect the pending transaction before retrying."
                    )
                originals[name] = original
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
    target = settings.providers.get(activate_provider_id)
    if target is None:
        return False, "Unknown provider. Add it before activating it."
    old_active = settings.active_provider_id
    config_path, settings_path = get_config_toml_path(), get_cmgr_settings_path()
    journal_path = None
    record = None
    config_before = settings_before = config_after = settings_after = None
    try:
        with mutation_lock(config_path), mutation_lock(settings_path):
            _recover_journals()
            config_before = read_optional(config_path)
            settings_before = read_optional(settings_path)
            if settings._loaded and settings_before != settings._source_bytes:
                raise FileConflictError("Settings changed; reload before activating.")
            config_after = prepare_configuration(target, config_before)
            settings.active_provider_id = activate_provider_id
            settings_after = serialize_settings(settings)
            transaction = get_backups_dir() / "transactions" / uuid.uuid4().hex
            record = {
                "state": "prepared",
                "before_config": _digest(config_before),
                "before_settings": _digest(settings_before),
                "after_config": _digest(config_after),
                "after_settings": _digest(settings_after),
            }
            for name, data in (("config", config_before), ("settings", settings_before)):
                if data is not None:
                    atomic_write(transaction / f"before-{name}.bin", data, expected=None)
            journal_path = transaction / "journal.json"
            _journal(journal_path, record)
            snapshot_configuration()
            try:
                atomic_write(config_path, config_after, expected=config_before)
                record["state"] = "config_applied"
                _journal(journal_path, record)
                commit_settings(settings, expected=settings_before)
            except (OSError, ValueError, RuntimeError):
                # Recovery is conditional: never roll back an unrelated external edit.
                _recover_journals()
                raise
            record["state"] = "committed"
            _journal(journal_path, record)
        return True, f"Selected {target.name}: {target.default_model}"
    except ConfigurationError as exc:
        message = str(exc)
    except FileConflictError as exc:
        message = str(exc)
    except (OSError, ValueError, RuntimeError):
        message = "Activation failed. Check permissions and pending recovery before retrying."
    if (
        journal_path is not None
        and read_optional(config_path) == config_after
        and read_optional(settings_path) == settings_after
    ):
        return (
            True,
            f"Selected {target.name}: {target.default_model}. Check the pending recovery marker.",
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
