"""Ephemeral native routing, credential separation, policy and owner cleanup."""

import os
from unittest.mock import Mock

import pytest
import tomlkit

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.core.adapters import launch
from codex_mgr.core.adapters.runtime import AdapterError
from codex_mgr.core.native_config import NativeConfiguration, verify_before_activation
from codex_mgr.core.profiles import ConfigurationError
from codex_mgr.utils.paths import get_codex_home, get_config_toml_path


def provider(**changes):
    values = {
        "id": "fixture",
        "name": "Synthetic",
        "default_model": 'exact/"quoted":model',
        "base_url": "https://upstream.invalid/v1",
        "wire_api": "chat",
        "auth_type": "env_var",
        "auth_token": "$SYNTHETIC_UPSTREAM_KEY",
        "env_http_headers": {"X-Synthetic": "SYNTHETIC_HEADER_KEY"},
    }
    values.update(changes)
    return ProviderConfig(**values)


@pytest.fixture
def boundaries(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-upstream-key")
    monkeypatch.setenv("SYNTHETIC_HEADER_KEY", "synthetic-header-key")
    runtime = Mock(base_url="http://127.0.0.1:12345/v1", token="synthetic-owner-token")
    runtime.__enter__ = Mock(return_value=runtime)
    factory = Mock(return_value=runtime)
    monkeypatch.setattr(launch, "AdapterRuntime", factory)
    view = NativeConfiguration({}, {}, {})
    monkeypatch.setattr(launch, "read_native_configuration", Mock(return_value=view))
    pre = Mock()
    post = Mock()
    monkeypatch.setattr(launch, "verify_before_activation", pre)
    monkeypatch.setattr(launch, "verify_after_activation", post)
    return runtime, factory, pre, post


def test_scope_holds_only_local_auth_and_preserves_durable_files(boundaries, monkeypatch, tmp_path):
    runtime, _, pre, post = boundaries
    home = get_codex_home()
    home.mkdir()
    config = get_config_toml_path()
    config.write_bytes(b'# original\nmodel="original"\n')
    original = config.read_bytes()
    settings = home / "cmgr.json"
    settings.write_bytes(b"synthetic original settings")
    environment = dict(os.environ)
    selected = provider()
    runner = Mock(return_value=7)
    monkeypatch.setattr(launch, "launch_codex", runner)
    with launch.AdapterLaunch(selected, cwd=tmp_path) as scope:
        args = scope.arguments
        child = scope.environment
        assert "SYNTHETIC_UPSTREAM_KEY" not in child
        assert "SYNTHETIC_HEADER_KEY" not in child
        assert "synthetic-owner-token" not in str(args)
        assert "synthetic-upstream-key" not in str(args)
        assert "synthetic-header-key" not in str(args)
        assert "upstream.invalid" not in str(args)
        generated = tomlkit.parse(pre.call_args.args[0].decode()).unwrap()
        identity = launch.adapter_connection_id(selected)
        table = generated["model_providers"][identity]
        assert generated["model"] == selected.default_model
        assert child[table["env_key"]] == runtime.token
        assert table["base_url"] == runtime.base_url
        assert table["supports_websockets"] is False
        assert generated["web_search"] == "disabled"
        assert generated["model_reasoning_summary"] == "none"
        assert post.call_args.kwargs["overrides"] == args[1::2]
        assert post.call_args.kwargs["environment"] == child
        assert scope.run(prompt="-exact & prompt") == 7
        assert runner.call_args.kwargs["extra_args"] == args + ["-C", str(tmp_path)]
        assert runner.call_args.kwargs["environment"] == child
        assert runner.call_args.kwargs["prompt"] == "-exact & prompt"
        child.clear()
        args.clear()
        assert scope.arguments and scope.environment
    runtime.close.assert_called_once()
    assert config.read_bytes() == original
    assert settings.read_bytes() == b"synthetic original settings"
    assert dict(os.environ) == environment
    assert sorted(p.name for p in home.iterdir()) == ["cmgr.json", "config.toml"]
    assert selected.auth_token == "$SYNTHETIC_UPSTREAM_KEY"
    with pytest.raises(AdapterError, match="not running"):
        _ = scope.environment
    with pytest.raises(AdapterError, match="twice"):
        scope.__enter__()


@pytest.mark.parametrize("stage", ["pre", "post"])
def test_verification_failure_closes_owner_without_publication(stage, boundaries):
    runtime, _, pre, post = boundaries
    (pre if stage == "pre" else post).side_effect = ConfigurationError("synthetic policy")
    with pytest.raises(ConfigurationError, match="policy"), launch.AdapterLaunch(provider()):
        pytest.fail("Invalid configuration was published.")
    runtime.close.assert_called_once()
    assert not get_codex_home().exists()


def test_existing_native_identity_is_preserved_and_never_started(boundaries, monkeypatch):
    runtime, factory, _, _ = boundaries
    selected = provider()
    view = NativeConfiguration(
        {"model_providers": {launch.adapter_connection_id(selected): {"env_key": "PRIVATE"}}},
        {},
        {},
    )
    monkeypatch.setattr(launch, "read_native_configuration", lambda **_: view)
    with pytest.raises(ConfigurationError, match="conflicts"), launch.AdapterLaunch(selected):
        pytest.fail("Existing definition reused.")
    factory.assert_not_called()
    runtime.close.assert_not_called()


@pytest.mark.parametrize("conflict", ["project", "required-provider", "chatgpt-only"])
def test_native_policy_and_project_conflicts_close_scope_before_launch(
    conflict, boundaries, monkeypatch
):
    runtime, _, _, post = boundaries
    view = NativeConfiguration({}, {}, {})
    if conflict == "project":
        view.config["model"] = "synthetic-project-model"
        view.origins["model"] = {"name": {"type": "project"}}
    elif conflict == "required-provider":
        view.requirements["modelProvider"] = "synthetic-managed-provider"
    else:
        view.requirements["allowedLoginMethods"] = ["chatgpt"]
    monkeypatch.setattr(launch, "read_native_configuration", lambda **_: view)
    monkeypatch.setattr(launch, "verify_before_activation", verify_before_activation)
    with pytest.raises(ConfigurationError), launch.AdapterLaunch(provider()):
        pytest.fail("Native conflict was ignored.")
    runtime.close.assert_called_once()
    post.assert_not_called()
    assert not get_codex_home().exists()


@pytest.mark.parametrize(
    "changes,fragment",
    [
        ({"wire_api": "responses"}, "does not need"),
        ({"model_selected": False}, "exact model"),
        ({"wire_api": "anthropic_proxy", "reasoning_effort": "high"}, "thinking budget"),
        ({"wire_api": "anthropic_proxy", "reasoning_budget": 1024}, "explicit native"),
        (
            {"wire_api": "anthropic_proxy", "reasoning_budget": 100, "reasoning_effort": "medium"},
            "1024",
        ),
        (
            {
                "wire_api": "anthropic_proxy",
                "reasoning_budget": 1024,
                "reasoning_effort": "medium",
                "max_output_tokens": 1024,
            },
            "below",
        ),
        ({"wire_api": "chat", "reasoning_budget": 1024}, "Messages"),
    ],
)
def test_unverified_controls_fail_before_owner_creation(changes, fragment, boundaries):
    _, factory, _, _ = boundaries
    with pytest.raises(ConfigurationError, match=fragment):
        launch.AdapterLaunch(provider(**changes))
    factory.assert_not_called()


def test_disabled_thinking_ignores_retained_budget_without_changing_settings(boundaries):
    _, factory, pre, _ = boundaries
    selected = provider(wire_api="anthropic_proxy", reasoning_enabled=False, reasoning_budget=2048)
    with launch.AdapterLaunch(selected):
        assert factory.call_args.args[0].reasoning_budget is None
        assert tomlkit.parse(pre.call_args.args[0].decode())["model_reasoning_effort"] == "none"
    assert selected.reasoning_budget == 2048
