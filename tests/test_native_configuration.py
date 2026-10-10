"""Policy, layer verification and conditional rollback use synthetic configuration."""

import json
from unittest.mock import Mock

import pytest

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import save_settings
from codex_mgr.core import activation, native_config
from codex_mgr.core.native_config import NativeConfiguration
from codex_mgr.core.profiles import ConfigurationError, parse_configuration, prepare_configuration
from codex_mgr.core.rpc import CodexRpcError
from codex_mgr.utils.paths import get_cmgr_settings_path, get_config_toml_path


def provider():
    return ProviderConfig(
        id="fixture",
        name="Fixture",
        base_url="https://fixture.invalid/v1",
        default_model="exact-model",
        auth_token="synthetic-private-key",
        reasoning_effort="high",
        context_window_limit=8192,
    )


def candidate():
    return prepare_configuration(provider(), b'model="old-model"\n')


def snapshot(config=None, origins=None, requirements=None):
    return NativeConfiguration(
        config if config is not None else parse_configuration(candidate()).unwrap(),
        origins or {},
        requirements or {},
    )


@pytest.mark.parametrize(
    "kind", ["project", "sessionFlags", "mdm", "legacyManagedConfig", "unknown"]
)
def test_higher_layer_model_conflict_is_rejected_before_writes(monkeypatch, kind):
    config = parse_configuration(candidate()).unwrap()
    config["model"] = "other-model"
    view = snapshot(config, {"model": {"name": {"type": kind}}})
    monkeypatch.setattr(native_config, "read_native_configuration", lambda **_: view)
    with pytest.raises(ConfigurationError, match="layer"):
        native_config.verify_before_activation(candidate())
    assert not get_config_toml_path().exists()


@pytest.mark.parametrize("kind", ["user", "system", "cloud"])
def test_lower_or_owned_layer_can_be_replaced(monkeypatch, kind):
    config = parse_configuration(candidate()).unwrap()
    config["model"] = "old-model"
    origin = {"name": {"type": kind, "file": str(get_config_toml_path()), "profile": None}}
    monkeypatch.setattr(
        native_config, "read_native_configuration", lambda **_: snapshot(config, {"model": origin})
    )
    native_config.verify_before_activation(candidate())


def test_named_user_profile_is_not_treated_as_the_base_file(monkeypatch):
    view = snapshot(
        {"model": "profile-model"},
        {
            "model": {
                "name": {"type": "user", "file": str(get_config_toml_path()), "profile": "work"}
            }
        },
    )
    monkeypatch.setattr(native_config, "read_native_configuration", lambda **_: view)
    with pytest.raises(ConfigurationError, match="layer"):
        native_config.verify_before_activation(candidate())


@pytest.mark.parametrize(
    "policy",
    [
        {"modelProvider": "required-provider"},
        {"modelProviders": {}},
        {"modelProviders": {"fixture": {"base_url": "https://other.invalid/v1"}}},
        {"allowedLoginMethods": ["chatgpt"]},
        {"allowedLoginMethods": []},
    ],
)
def test_managed_constraints_are_not_overridden(monkeypatch, policy):
    monkeypatch.setattr(
        native_config, "read_native_configuration", lambda **_: snapshot(requirements=policy)
    )
    with pytest.raises(ConfigurationError, match="[Mm]anaged policy"):
        native_config.verify_before_activation(candidate())


def test_matching_managed_definition_is_accepted(monkeypatch):
    config = parse_configuration(candidate()).unwrap()
    policy = {
        "modelProvider": "fixture",
        "modelProviders": config["model_providers"],
        "allowedLoginMethods": ["api"],
    }
    monkeypatch.setattr(
        native_config, "read_native_configuration", lambda **_: snapshot(requirements=policy)
    )
    native_config.verify_before_activation(candidate())
    native_config.verify_after_activation(candidate())


@pytest.mark.parametrize(
    "key,value",
    [
        ("model", "different-model"),
        ("model_provider", "different-provider"),
        ("model_reasoning_effort", "low"),
        ("model_context_window", 4096),
    ],
)
def test_post_write_checks_every_selection_control(monkeypatch, key, value):
    config = parse_configuration(candidate()).unwrap()
    config[key] = value
    monkeypatch.setattr(native_config, "read_native_configuration", lambda **_: snapshot(config))
    with pytest.raises(ConfigurationError, match="did not accept"):
        native_config.verify_after_activation(candidate())


@pytest.mark.parametrize(
    "key,value",
    [
        ("base_url", "https://other.invalid/v1"),
        ("wire_api", "chat"),
        ("experimental_bearer_token", "wrong-synthetic-key"),
        ("env_key", "UNEXPECTED_KEY"),
        ("requires_openai_auth", True),
        ("http_headers", {"X-Key": "other-secret"}),
        ("env_http_headers", {"X-Key": "OTHER_KEY"}),
    ],
)
def test_post_write_checks_endpoint_and_authentication(monkeypatch, key, value):
    config = parse_configuration(candidate()).unwrap()
    config["model_providers"]["fixture"][key] = value
    monkeypatch.setattr(native_config, "read_native_configuration", lambda **_: snapshot(config))
    with pytest.raises(ConfigurationError) as error:
        native_config.verify_after_activation(candidate())
    assert "secret" not in str(error.value) and "invalid/v1" not in str(error.value)


def test_legacy_profile_is_rejected_without_native_read(monkeypatch):
    read = Mock()
    monkeypatch.setattr(native_config, "read_native_configuration", read)
    encoded = prepare_configuration(provider(), b'profile="work"\n[profiles.work]\nmodel="old"\n')
    with pytest.raises(ConfigurationError, match="explicit migration"):
        native_config.verify_before_activation(encoded)
    read.assert_not_called()


def test_rpc_uses_cwd_and_no_credential_overrides(monkeypatch, tmp_path):
    reader = Mock()
    reader.__enter__ = Mock(return_value=reader)
    reader.__exit__ = Mock()
    reader.request.side_effect = [{"config": {}, "origins": {}}, {"requirements": None}]
    factory = Mock(return_value=reader)
    monkeypatch.setattr(native_config, "CodexReader", factory)
    result = native_config.read_native_configuration(cwd=tmp_path)
    factory.assert_called_once_with()
    assert reader.request.call_args_list[0].args == (
        "config/read",
        {"includeLayers": True, "cwd": str(tmp_path.resolve())},
    )
    assert result.requirements == {}


@pytest.mark.parametrize(
    "response",
    [
        {"config": None, "origins": {}},
        {"config": {}, "origins": []},
    ],
)
def test_invalid_rpc_shape_is_generic(monkeypatch, response):
    reader = Mock()
    reader.__enter__ = Mock(return_value=reader)
    reader.__exit__ = Mock()
    reader.request.side_effect = [response, {"requirements": None}]
    monkeypatch.setattr(native_config, "CodexReader", lambda: reader)
    with pytest.raises(ConfigurationError, match="could not verify"):
        native_config.read_native_configuration()


def test_rpc_error_does_not_echo_server_text(monkeypatch):
    reader = Mock()
    reader.__enter__ = Mock(side_effect=CodexRpcError("synthetic-private-error"))
    monkeypatch.setattr(native_config, "CodexReader", lambda: reader)
    with pytest.raises(ConfigurationError) as error:
        native_config.read_native_configuration()
    assert "synthetic-private-error" not in str(error.value)


def test_snapshot_representation_hides_sensitive_content():
    view = NativeConfiguration({"key": "synthetic-secret"}, {"path": "private-path"}, {})
    assert "secret" not in repr(view) and "private-path" not in repr(view)


def saved_state():
    config, settings_path = get_config_toml_path(), get_cmgr_settings_path()
    config.parent.mkdir(parents=True, exist_ok=True)
    before = b'model="old-model"\n'
    config.write_bytes(before)
    settings = CmgrSettings(providers={"fixture": provider()})
    save_settings(settings)
    return config, settings_path, settings, before, settings_path.read_bytes()


def test_rejected_native_postcheck_rolls_back_both_files(monkeypatch):
    config, settings_path, settings, before, saved = saved_state()

    def reject(*_, **__):
        raise ConfigurationError("Synthetic native rejection.")

    monkeypatch.setattr(activation, "verify_after_activation", reject)
    ok, message = activation.apply_settings(settings, activate_provider_id="fixture")
    assert not ok and message == "Synthetic native rejection."
    assert config.read_bytes() == before and settings_path.read_bytes() == saved
    assert settings.active_provider_id == "openai"
    assert activation.recover_pending_activation()[0]
    journals = list(config.parent.glob("backups/transactions/*/journal.json"))
    assert json.loads(journals[0].read_bytes())["state"] == "rolled_back"


def test_interruption_during_native_postcheck_never_commits_unverified_change(monkeypatch):
    config, settings_path, settings, before, saved = saved_state()

    def interrupt(*_, **__):
        raise KeyboardInterrupt

    monkeypatch.setattr(activation, "verify_after_activation", interrupt)
    with pytest.raises(KeyboardInterrupt):
        activation.apply_settings(settings, activate_provider_id="fixture")
    assert config.read_bytes() != before and settings_path.read_bytes() == saved
    assert activation.recover_pending_activation()[0]
    assert config.read_bytes() == before and settings_path.read_bytes() == saved


def test_external_change_during_native_verification_survives(monkeypatch):
    config, settings_path, settings, _, saved = saved_state()

    def change(*_, **__):
        config.write_bytes(b'model="external-model"\n')

    monkeypatch.setattr(activation, "verify_after_activation", change)
    assert not activation.apply_settings(settings, activate_provider_id="fixture")[0]
    assert config.read_bytes() == b'model="external-model"\n'
    assert settings_path.read_bytes() == saved
    assert not activation.recover_pending_activation()[0]


def test_preflight_failure_creates_no_snapshots(monkeypatch):
    config, settings_path, settings, before, saved = saved_state()

    def reject(*_, **__):
        raise ConfigurationError("Synthetic policy rejection.")

    monkeypatch.setattr(activation, "verify_before_activation", reject)
    assert not activation.apply_settings(settings, activate_provider_id="fixture")[0]
    assert config.read_bytes() == before and settings_path.read_bytes() == saved
    assert not (config.parent / "backups/config").exists()
    assert not (config.parent / "backups/transactions").exists()


@pytest.mark.parametrize("state", [[], "unknown", None])
def test_invalid_journal_state_preserves_current_files(state):
    config, settings_path, _, before, saved = saved_state()
    path = config.parent / "backups/transactions/fixture/journal.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"state": state}), encoding="utf-8")
    assert not activation.recover_pending_activation()[0]
    assert config.read_bytes() == before and settings_path.read_bytes() == saved


def test_duplicate_journal_fields_are_rejected_before_recovery():
    config, settings_path, _, before, saved = saved_state()
    path = config.parent / "backups/transactions/fixture/journal.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"state":"prepared","state":"committed"}')
    assert not activation.recover_pending_activation()[0]
    assert config.read_bytes() == before and settings_path.read_bytes() == saved


def test_external_journal_is_rejected_before_read(monkeypatch, tmp_path):
    config, settings_path, _, before, saved = saved_state()
    path = config.parent / "backups/transactions/fixture/journal.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"private":"external-data"}')
    original_resolve = type(path).resolve
    original_read = type(path).read_bytes
    reads = []

    def resolve(target, *args, **kwargs):
        return (
            tmp_path / "outside-private.json"
            if target == path
            else original_resolve(target, *args, **kwargs)
        )

    def read(target):
        reads.append(target)
        return original_read(target)

    monkeypatch.setattr(type(path), "resolve", resolve)
    monkeypatch.setattr(type(path), "read_bytes", read)
    assert not activation.recover_pending_activation()[0]
    assert path not in reads
    assert config.read_bytes() == before and settings_path.read_bytes() == saved


def test_external_preimage_is_rejected_before_read(monkeypatch, tmp_path):
    config, settings_path, settings, before, saved = saved_state()

    def interrupt(*_, **__):
        raise KeyboardInterrupt

    monkeypatch.setattr(activation, "verify_after_activation", interrupt)
    with pytest.raises(KeyboardInterrupt):
        activation.apply_settings(settings, activate_provider_id="fixture")
    path = next(config.parent.glob("backups/transactions/*/before-config.bin"))
    current = config.read_bytes()
    original_resolve = type(path).resolve
    original_read = type(path).read_bytes
    reads = []

    def resolve(target, *args, **kwargs):
        return (
            tmp_path / "outside-private.bin"
            if target == path
            else original_resolve(target, *args, **kwargs)
        )

    def read(target):
        reads.append(target)
        return original_read(target)

    monkeypatch.setattr(type(path), "resolve", resolve)
    monkeypatch.setattr(type(path), "read_bytes", read)
    assert not activation.recover_pending_activation()[0]
    assert path not in reads
    assert config.read_bytes() == current and current != before
    assert settings_path.read_bytes() == saved
