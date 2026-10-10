"""Contained, checksummed configuration revisions and reviewed restoration."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from codex_mgr.config.schema import CmgrSettings
from codex_mgr.config.settings import import_configuration, parse_settings, serialize_settings
from codex_mgr.core.activation import apply_configuration
from codex_mgr.core.native_config import validate_activation_configuration
from codex_mgr.core.profiles import ConfigurationError, parse_configuration, prepare_configuration
from codex_mgr.utils.files import FileConflictError, read_optional
from codex_mgr.utils.files import is_linked_path as _linked
from codex_mgr.utils.paths import get_cmgr_settings_path, get_codex_home, get_config_toml_path

RevisionKind = Literal["config", "settings"]
_LIMIT = 16 * 1024 * 1024
_STAMP = r"(?P<stamp>\d{8}T\d{6}\.\d{6}Z)_[a-f0-9]{32}"
_NAMES = {
    "config": re.compile(rf"(?:config|kept)_{_STAMP}\.toml"),
    "settings": re.compile(rf"(?:cmgr|kept)_{_STAMP}\.json"),
}


class RepairRequiredError(ValueError):
    """Current state cannot be parsed and requires an explicit repair choice."""


@dataclass(frozen=True)
class Revision:
    kind: RevisionKind
    name: str
    created_at: str
    size: int
    integrity: str
    digest: str | None = field(repr=False)


@dataclass(frozen=True)
class RevisionRestore:
    config_name: str | None
    settings_name: str | None
    repairing: bool
    configuration: bytes = field(repr=False)
    settings: bytes = field(repr=False)
    config_before: bytes | None = field(repr=False)
    settings_before: bytes | None = field(repr=False)


def _folder(kind: RevisionKind) -> Path:
    if kind not in _NAMES:
        raise ValueError("Choose config or settings revisions.")
    root = get_codex_home()
    folder = root / "backups" / kind
    for component in (root / "backups", folder):
        if (component.exists() or component.is_symlink()) and _linked(component):
            raise ValueError("Linked revision storage is not supported.")
        component.resolve().relative_to(root)
    return folder


def _path(kind: RevisionKind, name: str) -> Path:
    if not isinstance(name, str) or not _NAMES[kind].fullmatch(name):
        raise ValueError("Choose an exact revision filename from cmgr revisions list.")
    path = _folder(kind) / name
    for target in (path, path.with_name(path.name + ".sha256")):
        if _linked(target):
            raise ValueError("Linked revisions are not supported.")
        target.resolve().relative_to(path.parent.resolve())
    return path


def _bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as source:
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Revision exceeds the supported size.")
    return data


def _read(kind: RevisionKind, name: str) -> tuple[bytes, str]:
    path = _path(kind, name)
    data = _bounded(path, _LIMIT)
    checksum = _bounded(path.with_name(name + ".sha256"), 65)
    digest = hashlib.sha256(data).hexdigest()
    if checksum != digest.encode("ascii") + b"\n":
        raise ValueError("Revision integrity check failed.")
    return data, digest


def list_revisions(kind: RevisionKind | None = None) -> list[Revision]:
    """Read safe metadata without creating a home or exposing revision contents."""
    result = []
    for selected in (kind,) if kind is not None else ("config", "settings"):
        folder = _folder(selected)
        for path in sorted(folder.iterdir() if folder.exists() else [], reverse=True):
            match = _NAMES[selected].fullmatch(path.name)
            if match is None:
                continue
            try:
                stamp = datetime.strptime(match["stamp"], "%Y%m%dT%H%M%S.%fZ")
            except ValueError:
                continue
            size, digest, integrity = 0, None, "invalid"
            try:
                data, digest = _read(selected, path.name)
                size, integrity = len(data), "verified"
            except (OSError, ValueError):
                pass
            result.append(
                Revision(
                    selected,
                    path.name,
                    stamp.replace(tzinfo=timezone.utc).isoformat(),
                    size,
                    integrity,
                    digest,
                )
            )
    return result


def _selected(value: Revision | str, kind: RevisionKind) -> bytes:
    if isinstance(value, Revision):
        if value.kind != kind or value.integrity != "verified" or value.digest is None:
            raise ValueError("Choose a verified revision of the matching file kind.")
        data, digest = _read(kind, value.name)
        if digest != value.digest:
            raise FileConflictError("Revision changed after selection; inspect it again.")
        return data
    return _read(kind, value)[0]


def prepare_revision_restore(
    *,
    config: Revision | str | None = None,
    settings: Revision | str | None = None,
    repair: bool = False,
) -> RevisionRestore:
    """Validate and freeze a candidate before confirmation or any file mutation."""
    if config is None and settings is None:
        raise ValueError("Choose a config revision, settings revision, or both.")
    config_before = read_optional(get_config_toml_path())
    settings_before = read_optional(get_cmgr_settings_path())
    malformed_config = malformed_settings = False
    try:
        parse_configuration(config_before)
    except ConfigurationError:
        malformed_config = True
    try:
        current_settings = (
            parse_settings(settings_before) if settings_before is not None else CmgrSettings()
        )
    except ValueError:
        malformed_settings = True
        current_settings = None
    if (malformed_config or malformed_settings) and not repair:
        raise RepairRequiredError(
            "Current files are malformed. Select valid revisions and explicitly enable repair."
        )
    if malformed_config and config is None:
        raise ValueError(
            "Repair needs a config revision because current TOML cannot be preserved safely."
        )
    if malformed_settings and settings is None:
        raise ValueError(
            "Repair needs a settings revision because current manager state cannot be read."
        )
    candidate = (
        parse_settings(_selected(settings, "settings"))
        if settings is not None
        else current_settings
    )
    if config is not None:
        encoded = _selected(config, "config")
        import_configuration(candidate, parse_configuration(encoded).unwrap())
    else:
        provider = candidate.providers.get(candidate.active_provider_id)
        if provider is None:
            raise ValueError("The settings revision has no selected connection.")
        encoded = prepare_configuration(provider, config_before)
    validate_activation_configuration(encoded)
    return RevisionRestore(
        config.name if isinstance(config, Revision) else config,
        settings.name if isinstance(settings, Revision) else settings,
        repair,
        encoded,
        serialize_settings(candidate),
        config_before,
        settings_before,
    )


def apply_revision_restore(plan: RevisionRestore) -> tuple[bool, str]:
    """Use only frozen candidate bytes; expected preimages detect changes after review."""
    candidate = parse_settings(plan.settings)
    candidate._source_bytes = plan.settings_before
    candidate._loaded = True
    return apply_configuration(
        candidate,
        plan.configuration,
        expected_config=plan.config_before,
        repair_current=plan.repairing,
    )
