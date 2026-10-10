"""Keeping reviewed external edits settles only the chosen recovery transaction."""

import json
from unittest.mock import Mock

import pytest

from codex_mgr.cli import main
from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import save_settings
from codex_mgr.core import activation
from codex_mgr.core.doctor import run_diagnostics
from codex_mgr.tui import recovery_view
from codex_mgr.utils.files import read_snapshot
from codex_mgr.utils.paths import get_cmgr_settings_path, get_codex_home, get_config_toml_path


def external_conflict(monkeypatch):
    config = get_config_toml_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(b'model="original-model"\n')
    provider = ProviderConfig(
        id="fixture",
        name="Fixture",
        base_url="https://fixture.invalid/v1",
        default_model="requested-model",
        auth_type="none",
    )
    settings = CmgrSettings(providers={"fixture": provider})
    save_settings(settings)
    saved = get_cmgr_settings_path().read_bytes()

    def edit(*_, **__):
        config.write_bytes(b'model="external-model"\n# synthetic-private-comment\n')
        raise PermissionError("synthetic-private-error")

    with monkeypatch.context() as context:
        context.setattr(activation, "commit_settings", edit)
        assert not activation.apply_settings(settings, activate_provider_id="fixture")[0]
    assert get_cmgr_settings_path().read_bytes() == saved
    return activation.list_pending_activations()[0]


def journal(item):
    return get_codex_home() / "backups" / "transactions" / item.id / "journal.json"


def file_tree():
    return {
        p.relative_to(get_codex_home()).as_posix(): p.read_bytes()
        for p in get_codex_home().rglob("*")
        if p.is_file()
    }


def test_pending_inventory_is_read_only_and_redacted(monkeypatch):
    item = external_conflict(monkeypatch)
    before = file_tree()
    assert activation.list_pending_activations() == [item]
    assert file_tree() == before
    assert item.config_status == "external" and item.settings_status == "original"
    assert "private" not in repr(item) and item.config_digest not in repr(item)


def test_empty_inventory_does_not_create_home():
    assert activation.list_pending_activations() == []
    assert not get_codex_home().exists()


def test_explicit_keep_snapshots_current_files_and_settles_marker(monkeypatch):
    item = external_conflict(monkeypatch)
    config = get_config_toml_path().read_bytes()
    settings = get_cmgr_settings_path().read_bytes()
    assert not activation.recover_pending_activation()[0]
    ok, message = activation.accept_external_activation(item.id, expected=item.expected)
    assert ok and "kept" in message and "reload" in message
    assert get_config_toml_path().read_bytes() == config
    assert get_cmgr_settings_path().read_bytes() == settings
    assert activation.list_pending_activations() == []
    assert activation.recover_pending_activation()[0]
    record = json.loads(journal(item).read_bytes())
    assert record["state"] == "accepted_external"
    kept_config = next(get_codex_home().glob("backups/config/kept_*.toml"))
    kept_settings = next(get_codex_home().glob("backups/settings/kept_*.json"))
    assert read_snapshot(kept_config) == config and read_snapshot(kept_settings) == settings
    states = {check.name: check.status for check in run_diagnostics()}
    assert states["Activation recovery"] == "passed"
    assert not activation.accept_external_activation(item.id, expected=item.expected)[0]


def test_changed_files_require_another_review(monkeypatch):
    item = external_conflict(monkeypatch)
    config = b'model="newer-external-model"\n'
    get_config_toml_path().write_bytes(config)
    assert not activation.accept_external_activation(item.id, expected=item.expected)[0]
    assert get_config_toml_path().read_bytes() == config
    assert json.loads(journal(item).read_bytes())["state"] == item.state
    assert not list(get_codex_home().glob("backups/config/kept_*.toml"))


def test_snapshot_failure_keeps_recovery_pending(monkeypatch):
    item = external_conflict(monkeypatch)
    before = file_tree()
    backup = Mock(side_effect=PermissionError("synthetic-secret-path"))
    monkeypatch.setattr(activation, "create_snapshot", backup)
    ok, message = activation.accept_external_activation(item.id, expected=item.expected)
    assert not ok and "synthetic-secret-path" not in message
    assert file_tree() == before
    assert len(activation.list_pending_activations()) == 1


def test_external_change_while_snapshotting_is_preserved(monkeypatch):
    item = external_conflict(monkeypatch)
    original = activation.create_snapshot

    def snapshot(*args, **kwargs):
        result = original(*args, **kwargs)
        get_config_toml_path().write_bytes(b'model="latest-external-model"\n')
        return result

    monkeypatch.setattr(activation, "create_snapshot", snapshot)
    assert not activation.accept_external_activation(item.id, expected=item.expected)[0]
    assert get_config_toml_path().read_bytes() == b'model="latest-external-model"\n'
    assert activation.list_pending_activations()[0].id == item.id


@pytest.mark.parametrize("identity", ["", "../journal", "ABC", "f" * 31, None])
def test_keep_requires_exact_safe_transaction_id(identity):
    assert not activation.accept_external_activation(identity, expected=(None, None))[0]
    assert not get_codex_home().exists()


def test_corrupt_preimage_cannot_be_accepted(monkeypatch):
    item = external_conflict(monkeypatch)
    (journal(item).parent / "before-config.bin").write_bytes(b"corrupted")
    config = get_config_toml_path().read_bytes()
    assert not activation.accept_external_activation(item.id, expected=item.expected)[0]
    assert get_config_toml_path().read_bytes() == config
    assert json.loads(journal(item).read_bytes())["state"] != "accepted_external"


def test_cli_listing_and_keep_do_not_print_file_contents(monkeypatch, capsys):
    item = external_conflict(monkeypatch)
    assert main(["recover", "--list"]) == 0
    output = capsys.readouterr().out
    assert item.id in output and "external" in output
    assert "private" not in output and "external-model" not in output
    assert main(["recover", "--accept-external", item.id]) == 0
    assert activation.list_pending_activations() == []
    assert main(["recover", "--accept-external", item.id]) == 1


def test_cli_invalid_records_return_nonzero_without_raw_data():
    home = get_codex_home()
    path = home / "backups/transactions/fixture/journal.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"synthetic-private-data":')
    assert main(["recover", "--list"]) == 1


def test_tui_cancel_keeps_current_files_and_pending_marker(monkeypatch):
    item = external_conflict(monkeypatch)
    before = file_tree()
    selections = iter([item.id, None])
    monkeypatch.setattr(recovery_view, "select_menu", lambda *_, **__: next(selections))
    confirmation = Mock()
    confirmation.return_value.execute.return_value = False
    monkeypatch.setattr(recovery_view.inquirer, "confirm", confirmation)
    recovery_view.manage_activation_recovery()
    assert file_tree() == before
    assert confirmation.call_args.kwargs["default"] is False


def test_tui_keep_uses_the_shared_service(monkeypatch):
    item = external_conflict(monkeypatch)
    monkeypatch.setattr(recovery_view, "select_menu", lambda *_, **__: item.id)
    confirmation = Mock()
    confirmation.return_value.execute.return_value = True
    monkeypatch.setattr(recovery_view.inquirer, "confirm", confirmation)
    recovery_view.manage_activation_recovery()
    assert activation.list_pending_activations() == []
