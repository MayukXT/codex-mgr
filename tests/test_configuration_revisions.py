"""Revision integrity, reviewed preimages, repair and shared CLI/TUI restoration."""

import hashlib
import json
from unittest.mock import Mock

import pytest

from codex_mgr.cli import main
from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import load_settings, serialize_settings
from codex_mgr.core import activation, revisions
from codex_mgr.core.profiles import ConfigurationError, parse_configuration, prepare_configuration
from codex_mgr.tui import dashboard, revision_view
from codex_mgr.utils.files import create_snapshot, read_snapshot
from codex_mgr.utils.paths import get_cmgr_settings_path, get_codex_home, get_config_toml_path


def provider(model="saved-model"):
    return ProviderConfig(
        id="fixture",
        name="Fixture",
        base_url="https://fixture.invalid/v1",
        auth_type="none",
        default_model=model,
    )


@pytest.fixture
def saved_revisions():
    home = get_codex_home()
    home.mkdir(parents=True)
    selected = provider()
    settings = CmgrSettings(active_provider_id="fixture", providers={"fixture": selected})
    settings.preferences.ascii_mode = True
    config = prepare_configuration(
        selected, b'# saved comment\n[mcp_servers.fixture]\nurl="https://mcp.invalid"\n'
    )
    get_config_toml_path().write_bytes(config)
    get_cmgr_settings_path().write_bytes(serialize_settings(settings))
    config_path = create_snapshot(
        get_config_toml_path(), home / "backups/config", prefix="config_", suffix=".toml"
    )
    settings_path = create_snapshot(
        get_cmgr_settings_path(), home / "backups/settings", prefix="cmgr_", suffix=".json"
    )
    get_config_toml_path().write_bytes(
        prepare_configuration(provider("current-model"), b"# current comment\n")
    )
    settings.providers["fixture"].default_model = "current-model"
    settings.preferences.ascii_mode = False
    get_cmgr_settings_path().write_bytes(serialize_settings(settings))
    listed = revisions.list_revisions()
    return (
        next(item for item in listed if item.name == config_path.name),
        next(item for item in listed if item.name == settings_path.name),
        config,
    )


def current():
    return get_config_toml_path().read_bytes(), get_cmgr_settings_path().read_bytes()


def tree():
    return {
        p.relative_to(get_codex_home()).as_posix(): p.read_bytes()
        for p in get_codex_home().rglob("*")
        if p.is_file()
    }


def test_empty_inventory_is_read_only():
    assert revisions.list_revisions() == []
    assert main(["revisions", "list"]) == 0
    assert not get_codex_home().exists()


def test_inventory_never_exposes_contents_or_private_paths(saved_revisions, capsys):
    config, settings, _ = saved_revisions
    path = get_codex_home() / "backups/config" / config.name
    data = path.read_bytes() + b"# synthetic-private-content\n"
    path.write_bytes(data)
    path.with_name(path.name + ".sha256").write_bytes(
        hashlib.sha256(data).hexdigest().encode() + b"\n"
    )
    before = tree()
    assert main(["revisions", "list"]) == 0
    output = capsys.readouterr().out
    assert config.name in output and settings.name in output and "verified" in output
    assert "synthetic-private-content" not in output and str(get_codex_home()) not in output
    assert tree() == before
    assert config.digest not in repr(config)


def test_config_restore_keeps_exact_bytes_and_reconciles_manager(saved_revisions):
    config, _, raw = saved_revisions
    before = current()
    plan = revisions.prepare_revision_restore(config=config)
    assert current() == before
    ok, message = revisions.apply_revision_restore(plan)
    assert ok, message
    assert get_config_toml_path().read_bytes() == raw
    restored = load_settings()
    assert restored.active_provider_id == "fixture"
    assert restored.providers["fixture"].default_model == "saved-model"
    assert restored.preferences.ascii_mode is False
    assert before[0] in [
        read_snapshot(p) for p in get_codex_home().glob("backups/config/config_*.toml")
    ]
    assert before[1] in [
        read_snapshot(p) for p in get_codex_home().glob("backups/settings/cmgr_*.json")
    ]


def test_settings_restore_applies_selected_connection_preserving_unowned_toml(saved_revisions):
    _, settings, _ = saved_revisions
    plan = revisions.prepare_revision_restore(settings=settings)
    assert revisions.apply_revision_restore(plan)[0]
    assert b"# current comment" in get_config_toml_path().read_bytes()
    assert load_settings().preferences.ascii_mode is True
    assert parse_configuration(get_config_toml_path().read_bytes())["model"] == "saved-model"


def test_both_restore_uses_config_selection_and_saved_manager_preferences(saved_revisions):
    config, settings, _ = saved_revisions
    path = get_codex_home() / "backups/settings" / settings.name
    data = json.loads(path.read_bytes())
    data["providers"]["fixture"]["default_model"] = "different-saved-model"
    raw = json.dumps(data).encode()
    path.write_bytes(raw)
    path.with_name(path.name + ".sha256").write_bytes(
        hashlib.sha256(raw).hexdigest().encode() + b"\n"
    )
    plan = revisions.prepare_revision_restore(config=config, settings=settings.name)
    assert revisions.apply_revision_restore(plan)[0]
    restored = load_settings()
    assert restored.providers["fixture"].default_model == "saved-model"
    assert restored.preferences.ascii_mode is True


@pytest.mark.parametrize("changed", ["config", "settings"])
def test_reviewed_restore_preserves_later_external_changes(saved_revisions, changed):
    config, settings, _ = saved_revisions
    plan = revisions.prepare_revision_restore(config=config, settings=settings)
    path = get_config_toml_path() if changed == "config" else get_cmgr_settings_path()
    path.write_bytes(b"synthetic external edit")
    before = current()
    assert not revisions.apply_revision_restore(plan)[0]
    assert current() == before
    assert not list(get_codex_home().glob("backups/transactions/*/journal.json"))


@pytest.mark.parametrize("kind", ["config", "settings"])
def test_malformed_files_need_explicit_repair_and_matching_revision(saved_revisions, kind):
    config, settings, _ = saved_revisions
    path = get_config_toml_path() if kind == "config" else get_cmgr_settings_path()
    path.write_bytes(b"malformed synthetic current file")
    before = tree()
    with pytest.raises(revisions.RepairRequiredError):
        revisions.prepare_revision_restore(config=config, settings=settings)
    with pytest.raises(ValueError, match="Repair needs"):
        revisions.prepare_revision_restore(
            config=config if kind == "settings" else None,
            settings=settings if kind == "config" else None,
            repair=True,
        )
    assert tree() == before
    plan = revisions.prepare_revision_restore(config=config, settings=settings, repair=True)
    assert plan.repairing is True
    assert revisions.apply_revision_restore(plan)[0]
    assert load_settings().providers["fixture"].default_model == "saved-model"
    suffix, prefix = ("toml", "config") if kind == "config" else ("json", "cmgr")
    folder = "config" if kind == "config" else "settings"
    assert b"malformed synthetic current file" in [
        read_snapshot(p) for p in get_codex_home().glob(f"backups/{folder}/{prefix}_*.{suffix}")
    ]


def test_manager_repair_still_runs_native_preflight(saved_revisions, monkeypatch):
    config, settings, _ = saved_revisions
    get_cmgr_settings_path().write_bytes(b"malformed JSON")
    plan = revisions.prepare_revision_restore(config=config, settings=settings, repair=True)
    before = current()
    check = Mock(side_effect=ConfigurationError("Synthetic managed policy conflict."))
    monkeypatch.setattr(activation, "verify_before_activation", check)
    assert not revisions.apply_revision_restore(plan)[0]
    check.assert_called_once_with(plan.configuration)
    assert current() == before
    assert not list(get_codex_home().glob("backups/transactions/*/journal.json"))


@pytest.mark.parametrize("repair", [False, True])
def test_native_read_failure_needs_explicit_repair(saved_revisions, monkeypatch, repair):
    from codex_mgr.core.native_config import NativeConfigurationReadError

    config, _, _ = saved_revisions
    plan = revisions.prepare_revision_restore(config=config, repair=repair)
    before = current()
    preflight = Mock(
        side_effect=NativeConfigurationReadError("Synthetic unreadable native config.")
    )
    monkeypatch.setattr(activation, "verify_before_activation", preflight)
    postflight = activation.verify_after_activation
    after = Mock(wraps=postflight)
    monkeypatch.setattr(activation, "verify_after_activation", after)
    ok, message = revisions.apply_revision_restore(plan)
    assert ok is repair, message
    preflight.assert_called_once()
    if repair:
        after.assert_called_once_with(plan.configuration)
    else:
        assert current() == before
        after.assert_not_called()


def test_read_failure_repair_cannot_commit_without_native_postflight(saved_revisions, monkeypatch):
    from codex_mgr.core.native_config import NativeConfigurationReadError

    config, _, _ = saved_revisions
    plan = revisions.prepare_revision_restore(config=config, repair=True)
    before = current()
    monkeypatch.setattr(
        activation, "verify_before_activation", Mock(side_effect=NativeConfigurationReadError)
    )
    monkeypatch.setattr(
        activation, "verify_after_activation", Mock(side_effect=NativeConfigurationReadError)
    )
    assert not revisions.apply_revision_restore(plan)[0]
    assert current() == before


def test_native_postflight_rejection_restores_exact_malformed_preimages(
    saved_revisions, monkeypatch
):
    config, settings, _ = saved_revisions
    get_config_toml_path().write_bytes(b"invalid TOML")
    get_cmgr_settings_path().write_bytes(b"invalid JSON")
    before = current()
    plan = revisions.prepare_revision_restore(config=config, settings=settings, repair=True)
    preflight = Mock(side_effect=AssertionError("Cannot inspect malformed current TOML."))
    monkeypatch.setattr(activation, "verify_before_activation", preflight)
    monkeypatch.setattr(
        activation,
        "verify_after_activation",
        Mock(side_effect=ConfigurationError("Synthetic rejected selection.")),
    )
    assert not revisions.apply_revision_restore(plan)[0]
    assert current() == before
    preflight.assert_not_called()
    record = json.loads(
        next(get_codex_home().glob("backups/transactions/*/journal.json")).read_bytes()
    )
    assert record["state"] == "rolled_back"


def test_external_edit_during_verification_remains_pending(saved_revisions, monkeypatch):
    config, _, _ = saved_revisions
    plan = revisions.prepare_revision_restore(config=config)
    original_settings = get_cmgr_settings_path().read_bytes()

    def reject(*_):
        get_config_toml_path().write_bytes(b'# synthetic external\nmodel="external-model"\n')
        raise ConfigurationError("Synthetic rejected selection.")

    monkeypatch.setattr(activation, "verify_after_activation", reject)
    assert not revisions.apply_revision_restore(plan)[0]
    assert b"synthetic external" in get_config_toml_path().read_bytes()
    assert get_cmgr_settings_path().read_bytes() == original_settings
    assert len(activation.list_pending_activations()) == 1


def test_backup_failure_prevents_any_current_replacement(saved_revisions, monkeypatch):
    config, settings, _ = saved_revisions
    plan = revisions.prepare_revision_restore(config=config, settings=settings)
    before = current()
    monkeypatch.setattr(
        activation, "create_snapshot", Mock(side_effect=PermissionError("synthetic private error"))
    )
    ok, message = revisions.apply_revision_restore(plan)
    assert not ok and "synthetic private error" not in message
    assert current() == before
    assert activation.recover_pending_activation()[0]


def test_interrupted_repair_can_recover_both_malformed_originals(saved_revisions, monkeypatch):
    config, settings, _ = saved_revisions
    get_config_toml_path().write_bytes(b"invalid TOML")
    get_cmgr_settings_path().write_bytes(b"invalid JSON")
    before = current()
    plan = revisions.prepare_revision_restore(config=config, settings=settings, repair=True)
    monkeypatch.setattr(activation, "verify_after_activation", Mock(side_effect=KeyboardInterrupt))
    with pytest.raises(KeyboardInterrupt):
        revisions.apply_revision_restore(plan)
    assert get_config_toml_path().read_bytes() == plan.configuration
    assert activation.recover_pending_activation()[0]
    assert current() == before


@pytest.mark.parametrize("mutation", ["corrupt", "missing_checksum", "changed_pair"])
def test_snapshot_changes_or_missing_integrity_stop_preparation(saved_revisions, mutation):
    config, _, _ = saved_revisions
    path = get_codex_home() / "backups/config" / config.name
    if mutation == "missing_checksum":
        path.with_name(path.name + ".sha256").unlink()
    else:
        path.write_bytes(b'# replacement\nmodel="replacement-model"\n')
        if mutation == "changed_pair":
            path.with_name(path.name + ".sha256").write_bytes(
                hashlib.sha256(path.read_bytes()).hexdigest().encode() + b"\n"
            )
    before = current()
    with pytest.raises((ValueError, OSError, RuntimeError)):
        revisions.prepare_revision_restore(config=config)
    assert current() == before
    if mutation != "changed_pair":
        assert revisions.list_revisions("config")[0].integrity == "invalid"


@pytest.mark.parametrize(
    "name",
    [
        "../config.toml",
        "config.toml",
        "/tmp/config.toml",
        "C:\\private\\config.toml",
        "bad\nname",
        "",
        None,
    ],
)
def test_names_require_exact_checksummed_revisions(name):
    with pytest.raises(ValueError):
        revisions.prepare_revision_restore(config=name)
    assert not get_codex_home().exists()


def test_oversized_revision_read_is_bounded(saved_revisions, monkeypatch):
    config, _, _ = saved_revisions
    monkeypatch.setattr(revisions, "_LIMIT", 8)
    with pytest.raises(ValueError, match="size"):
        revisions.prepare_revision_restore(config=config.name)


@pytest.mark.parametrize("target", ["file", "checksum", "folder"])
def test_linked_revision_components_are_rejected_before_read(saved_revisions, monkeypatch, target):
    config, _, _ = saved_revisions
    path = get_codex_home() / "backups/config" / config.name
    selected = {
        "file": path,
        "checksum": path.with_name(path.name + ".sha256"),
        "folder": path.parent,
    }[target]
    original = revisions._linked
    monkeypatch.setattr(revisions, "_linked", lambda path: path == selected or original(path))
    before = current()
    with pytest.raises(ValueError, match="Linked"):
        revisions.prepare_revision_restore(config=config.name)
    assert current() == before


def test_real_checksum_symlink_cannot_read_an_outside_file(saved_revisions, tmp_path):
    config, _, _ = saved_revisions
    path = get_codex_home() / "backups/config" / config.name
    checksum = path.with_name(path.name + ".sha256")
    outside = tmp_path / "synthetic-outside-checksum"
    outside.write_bytes(checksum.read_bytes())
    checksum.unlink()
    try:
        checksum.symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks requires permission on this platform.")
    before = current()
    with pytest.raises(ValueError, match="Linked"):
        revisions.prepare_revision_restore(config=config.name)
    assert current() == before


def test_missing_model_and_controls_do_not_inherit_stale_manager_selection(saved_revisions):
    config, _, _ = saved_revisions
    path = get_codex_home() / "backups/config" / config.name
    raw = b"# Codex owns the default model and controls\n"
    path.write_bytes(raw)
    path.with_name(path.name + ".sha256").write_bytes(
        hashlib.sha256(raw).hexdigest().encode() + b"\n"
    )
    plan = revisions.prepare_revision_restore(config=config.name)
    restored = CmgrSettings.model_validate_json(plan.settings)
    assert restored.active_provider_id == "openai"
    assert not restored.providers["openai"].model_selected
    assert restored.providers["openai"].reasoning_effort is None
    assert revisions.apply_revision_restore(plan)[0]
    assert get_config_toml_path().read_bytes() == raw


def test_imported_signin_does_not_overwrite_an_existing_manager_id(saved_revisions):
    config, _, _ = saved_revisions
    api = provider().model_copy(update={"id": "openai"})
    unrelated = provider().model_copy(update={"id": "openai_oauth", "name": "Keep this API"})
    current_settings = CmgrSettings(providers={"openai": api, "openai_oauth": unrelated})
    get_cmgr_settings_path().write_bytes(serialize_settings(current_settings))
    path = get_codex_home() / "backups/config" / config.name
    raw = b'model="signin-model"\n'
    path.write_bytes(raw)
    path.with_name(path.name + ".sha256").write_bytes(
        hashlib.sha256(raw).hexdigest().encode() + b"\n"
    )
    plan = revisions.prepare_revision_restore(config=config.name)
    assert revisions.apply_revision_restore(plan)[0]
    restored = load_settings()
    assert restored.providers["openai"].auth_type == "none"
    assert restored.providers["openai_oauth"].name == "Keep this API"
    assert restored.active_provider_id == "openai_oauth_imported"
    assert restored.providers[restored.active_provider_id].auth_type == "official_oauth"


def test_reserved_builtin_override_is_rejected_without_rewriting_history(saved_revisions):
    config, _, _ = saved_revisions
    existing = CmgrSettings(
        providers={
            "openai": ProviderConfig(
                id="openai",
                name="Sign-in",
                base_url="https://api.openai.com/v1",
                auth_type="official_oauth",
            )
        }
    )
    get_cmgr_settings_path().write_bytes(serialize_settings(existing))
    path = get_codex_home() / "backups/config" / config.name
    raw = b'model="saved-model"\nmodel_provider="openai"\nforced_login_method="api"\n[model_providers.openai]\nname="Custom API"\nbase_url="https://fixture.invalid/v1"\nwire_api="responses"\nrequires_openai_auth=false\nenv_key="SYNTHETIC_KEY"\n'
    path.write_bytes(raw)
    path.with_name(path.name + ".sha256").write_bytes(
        hashlib.sha256(raw).hexdigest().encode() + b"\n"
    )
    before = tree()
    with pytest.raises(ConfigurationError, match="reserves"):
        revisions.prepare_revision_restore(config=config.name)
    assert tree() == before


def test_cli_restores_exact_files_and_rejects_invalid_selection(saved_revisions, capsys):
    config, settings, raw = saved_revisions
    assert main(["revisions", "restore", "--config", config.name, "--settings", settings.name]) == 0
    assert get_config_toml_path().read_bytes() == raw
    before = current()
    assert main(["revisions", "restore", "--config", "../config.toml"]) == 1
    assert current() == before
    assert str(get_codex_home()) not in capsys.readouterr().out


def test_tui_refusal_preserves_files_and_uses_no_as_default(saved_revisions, monkeypatch):
    config, _, _ = saved_revisions
    monkeypatch.setattr(revision_view, "select_menu", lambda *_, **__: "config")
    monkeypatch.setattr(revision_view, "pick_revision", lambda _: config)
    confirm = Mock()
    confirm.return_value.execute.return_value = False
    monkeypatch.setattr(revision_view.inquirer, "confirm", confirm)
    before = tree()
    revision_view.manage_configuration_revisions()
    assert tree() == before
    assert confirm.call_args.kwargs["default"] is False


def test_tui_repair_and_restore_need_separate_explicit_choices(saved_revisions, monkeypatch):
    config, settings, _ = saved_revisions
    get_config_toml_path().write_bytes(b"invalid TOML")
    get_cmgr_settings_path().write_bytes(b"invalid JSON")
    monkeypatch.setattr(revision_view, "select_menu", lambda *_, **__: "both")
    monkeypatch.setattr(
        revision_view, "pick_revision", lambda kind: config if kind == "config" else settings
    )
    confirm = Mock()
    confirm.return_value.execute.side_effect = [True, False]
    monkeypatch.setattr(revision_view.inquirer, "confirm", confirm)
    before = tree()
    revision_view.manage_configuration_revisions()
    assert tree() == before
    assert confirm.call_count == 2
    assert all(call.kwargs["default"] is False for call in confirm.call_args_list)


def test_tui_confirmation_uses_frozen_review_and_detects_changed_current_files(
    saved_revisions, monkeypatch
):
    config, _, _ = saved_revisions
    monkeypatch.setattr(revision_view, "select_menu", lambda *_, **__: "config")
    monkeypatch.setattr(revision_view, "pick_revision", lambda _: config)
    confirm = Mock()

    def external_edit():
        get_config_toml_path().write_bytes(b'model="external-model"\n')
        return True

    confirm.return_value.execute.side_effect = external_edit
    monkeypatch.setattr(revision_view.inquirer, "confirm", confirm)
    revision_view.manage_configuration_revisions()
    assert get_config_toml_path().read_bytes() == b'model="external-model"\n'


def test_dashboard_offers_recovery_when_settings_are_malformed(monkeypatch):
    monkeypatch.setattr(dashboard, "load_settings", Mock(side_effect=ValueError))
    monkeypatch.setattr(dashboard, "select_menu", Mock(side_effect=["revisions", None]))
    restore = Mock()
    monkeypatch.setattr(revision_view, "manage_configuration_revisions", restore)
    dashboard.run_main_dashboard()
    restore.assert_called_once_with()
    assert not get_codex_home().exists()
