"""Configuration changes must preserve unrelated data and commit coherently."""

import json
from pathlib import Path

import pytest

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import load_settings, save_settings
from codex_mgr.core.profiles import read_config_toml, switch_active_provider


@pytest.fixture
def codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    return tmp_path


def gateway(**kwargs):
    return ProviderConfig(
        id="gateway",
        name="Gateway",
        base_url="https://gateway.example/v1",
        default_model="coding-model",
        auth_type="env_var",
        auth_token="$SYNTHETIC_KEY",
        **kwargs,
    )


def test_malformed_toml_is_never_overwritten(codex_home):
    path = codex_home / "config.toml"
    original = b'[mcp_servers.example\nurl = "https://mcp.example"\n'
    path.write_bytes(original)
    ok, _ = switch_active_provider(gateway())
    assert not ok
    assert path.read_bytes() == original
    assert not (codex_home / "backups/config").exists()


def test_comments_headers_and_notice_state_survive(codex_home):
    path = codex_home / "config.toml"
    path.write_text(
        """# user heading
model = "old" # user comment
[model_providers.gateway]
name = "Old label"
request_max_retries = 7 # keep retry
[model_providers.gateway.http_headers]
X-Custom = "value"
[tui.model_availability_nux]
old = 4
[mcp_servers.example]
url = "https://mcp.example"
""",
        encoding="utf-8",
    )
    ok, _ = switch_active_provider(gateway())
    assert ok
    text = path.read_text(encoding="utf-8")
    assert "# user heading" in text and "# user comment" in text and "# keep retry" in text
    data = read_config_toml()
    table = data["model_providers"]["gateway"]
    assert table["request_max_retries"] == 7
    assert table["http_headers"]["X-Custom"] == "value"
    assert table["env_key"] == "SYNTHETIC_KEY"
    assert "bearer_token_env_var" not in table
    assert data["tui"]["model_availability_nux"]["old"] == 4


def test_legacy_controls_are_not_emitted(codex_home):
    (codex_home / "config.toml").write_text('model = "old"\n', encoding="utf-8")
    ok, _ = switch_active_provider(gateway(context_window_limit=8192))
    assert ok
    data = read_config_toml()
    assert data["model_context_window"] == 8192
    assert not {"model_reasoning", "stream", "max_tokens"} & data.keys()
    assert (
        not {"stream", "context_window", "max_tokens", "reasoning_budget"}
        & data["model_providers"]["gateway"].keys()
    )


def test_openai_api_key_and_endpoint_reach_custom_connection(codex_home):
    (codex_home / "config.toml").write_text('model = "old"\n', encoding="utf-8")
    provider = ProviderConfig(
        id="openai",
        name="OpenAI API",
        base_url="https://api.example/v1",
        auth_token="synthetic-api-token",
        default_model="coding-model",
    )
    ok, _ = switch_active_provider(provider)
    assert ok
    data = read_config_toml()
    assert data["model_provider"] != "openai"
    table = data["model_providers"][data["model_provider"]]
    assert table["base_url"] == "https://api.example/v1"
    assert table["experimental_bearer_token"] == "synthetic-api-token"


def test_legacy_profile_is_preserved_until_explicit_migration(codex_home):
    (codex_home / "config.toml").write_text(
        """model = "global"
profile = "work"
[profiles.work]
model = "profile-old"
[profiles.other]
model = "other"
""",
        encoding="utf-8",
    )
    before = (codex_home / "config.toml").read_bytes()
    ok, message = switch_active_provider(gateway())
    assert not ok and "migration" in message
    assert (codex_home / "config.toml").read_bytes() == before
    data = read_config_toml()
    assert data["model"] == "global"
    assert data["profiles"]["work"]["model"] == "profile-old"
    assert data["profiles"]["other"]["model"] == "other"


def test_settings_reads_do_not_create_files(codex_home):
    settings = load_settings()
    assert settings.providers
    assert not (codex_home / "cmgr.json").exists()


def test_corrupt_settings_raise_without_defaults_overwrite(codex_home):
    path = codex_home / "cmgr.json"
    path.write_bytes(b'{"providers":')
    with pytest.raises(ValueError, match="settings"):
        load_settings()
    assert path.read_bytes() == b'{"providers":'


def test_backup_failure_blocks_settings_save(codex_home, monkeypatch):
    path = codex_home / "cmgr.json"
    settings = CmgrSettings(active_provider_id="gateway", providers={"gateway": gateway()})
    save_settings(settings)
    original = path.read_bytes()
    settings.providers["gateway"].name = "Changed"

    def fail(*args, **kwargs):
        raise PermissionError("synthetic permission error")

    monkeypatch.setattr("codex_mgr.config.settings.create_snapshot", fail)
    with pytest.raises(PermissionError):
        save_settings(settings)
    assert path.read_bytes() == original


def test_stale_settings_cannot_overwrite_newer_changes(codex_home):
    settings = CmgrSettings(active_provider_id="gateway", providers={"gateway": gateway()})
    save_settings(settings)
    a, b = load_settings(), load_settings()
    a.providers["gateway"].name = "First edit"
    save_settings(a)
    b.providers["gateway"].name = "Stale edit"
    with pytest.raises(RuntimeError, match="changed"):
        save_settings(b)
    assert (
        json.loads((codex_home / "cmgr.json").read_text())["providers"]["gateway"]["name"]
        == "First edit"
    )


def test_failed_activation_rolls_back_config_and_saved_state(codex_home, monkeypatch):
    from codex_mgr.core.activation import activate_provider

    path = codex_home / "config.toml"
    path.write_bytes(b'model = "old"\n')
    settings = CmgrSettings(
        providers={
            "openai": ProviderConfig(
                id="openai",
                name="ChatGPT",
                base_url="https://api.openai.com/v1",
                auth_type="official_oauth",
            ),
            "gateway": gateway(),
        }
    )
    save_settings(settings)
    before = (codex_home / "cmgr.json").read_bytes()

    def fail(*args, **kwargs):
        raise PermissionError("synthetic permission error")

    monkeypatch.setattr("codex_mgr.core.activation.commit_settings", fail)
    ok, _ = activate_provider("gateway")
    assert not ok
    assert path.read_bytes() == b'model = "old"\n'
    assert (codex_home / "cmgr.json").read_bytes() == before


def test_initial_settings_import_existing_custom_connection(codex_home):
    (codex_home / "config.toml").write_text(
        """model = "custom-model"
model_provider = "existing_gateway"
[model_providers.existing_gateway]
name = "Existing"
base_url = "https://existing.example/api"
env_key = "SYNTHETIC_EXISTING_KEY"
wire_api = "responses"
""",
        encoding="utf-8",
    )
    settings = load_settings()
    assert settings.active_provider_id == "existing_gateway"
    provider = settings.providers["existing_gateway"]
    assert provider.default_model == "custom-model"
    assert provider.base_url == "https://existing.example/api"
    assert provider.auth_token == "$SYNTHETIC_EXISTING_KEY"


def test_emitted_configuration_matches_official_schema(codex_home):
    from jsonschema.validators import validator_for

    path = codex_home / "config.toml"
    path.write_text('model = "old"\n', encoding="utf-8")
    assert switch_active_provider(gateway())[0]
    schema = json.loads(
        (Path(__file__).parent / "fixtures/codex-0.159.0-config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator_for(schema)(schema).validate(read_config_toml())


@pytest.mark.parametrize("content", ['model_providers = "invalid"\n', "profiles = 10\n"])
def test_invalid_config_tables_abort_before_mutation(codex_home, content):
    path = codex_home / "config.toml"
    path.write_text(content, encoding="utf-8")
    assert not switch_active_provider(gateway())[0]
    assert path.read_text(encoding="utf-8") == content


def test_cli_provider_list_and_failed_switch_have_meaningful_status(codex_home):
    from codex_mgr.cli import main

    assert main(["provider", "list"]) == 0
    assert main(["switch", "unknown-provider"]) == 1
    assert not (codex_home / "cmgr.json").exists()


def test_activation_refuses_to_overwrite_external_edit_on_rollback(codex_home, monkeypatch):
    from codex_mgr.core.activation import activate_provider, recover_pending_activation

    path = codex_home / "config.toml"
    path.write_bytes(b'model = "old"\n')
    settings = CmgrSettings(providers={"gateway": gateway()})
    save_settings(settings)
    before = (codex_home / "cmgr.json").read_bytes()

    def fail(*args, **kwargs):
        path.write_bytes(b'model = "external-edit"\n')
        raise PermissionError("synthetic error")

    monkeypatch.setattr("codex_mgr.core.activation.commit_settings", fail)
    assert not activate_provider("gateway")[0]
    assert path.read_bytes() == b'model = "external-edit"\n'
    assert (codex_home / "cmgr.json").read_bytes() == before
    assert not recover_pending_activation()[0]


def test_interrupted_activation_recovers_after_writer_releases_lock(codex_home, monkeypatch):
    from codex_mgr.core.activation import activate_provider, recover_pending_activation

    path = codex_home / "config.toml"
    path.write_bytes(b'model = "old"\n')
    settings = CmgrSettings(providers={"gateway": gateway()})
    save_settings(settings)
    before = (codex_home / "cmgr.json").read_bytes()

    def interrupted(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr("codex_mgr.core.activation.commit_settings", interrupted)
    with pytest.raises(KeyboardInterrupt):
        activate_provider("gateway")
    assert read_config_toml()["model"] == "coding-model"
    assert recover_pending_activation()[0]
    assert path.read_bytes() == b'model = "old"\n'
    assert (codex_home / "cmgr.json").read_bytes() == before


def test_successful_activation_commits_both_files(codex_home):
    from codex_mgr.core.activation import activate_provider

    (codex_home / "config.toml").write_bytes(b'model = "old"\n')
    save_settings(CmgrSettings(providers={"gateway": gateway()}))
    assert activate_provider("gateway")[0]
    assert read_config_toml()["model_provider"] == "gateway"
    assert json.loads((codex_home / "cmgr.json").read_text())["active_provider_id"] == "gateway"


def test_saved_api_key_configuration_preserves_existing_oauth_identity(codex_home):
    (codex_home / "config.toml").write_bytes(b'model = "current-model"\n')
    api = ProviderConfig(
        id="openai",
        name="OpenAI API",
        base_url="https://api.openai.com/v1",
        auth_token="synthetic-token",
    )
    save_settings(CmgrSettings(providers={"openai": api}))
    settings = load_settings()
    assert settings.providers["openai"].auth_token == "synthetic-token"
    assert settings.providers[settings.active_provider_id].auth_type == "official_oauth"


def test_duplicate_codex_identity_prefers_the_saved_active_connection(codex_home):
    first = gateway().model_copy(update={"id": "first", "codex_provider_id": "shared"})
    second = gateway().model_copy(update={"id": "second", "codex_provider_id": "shared"})
    save_settings(
        CmgrSettings(active_provider_id="second", providers={"first": first, "second": second})
    )
    (codex_home / "config.toml").write_text(
        'model="native-model"\nmodel_provider="shared"\n[model_providers.shared]\nname="Shared"\nbase_url="https://fixture.invalid/v1"\nwire_api="responses"\n',
        encoding="utf-8",
    )
    loaded = load_settings()
    assert loaded.active_provider_id == "second"
    assert loaded.providers["second"].default_model == "native-model"
    assert loaded.providers["first"].default_model == "coding-model"
    assert loaded.providers["second"].auth_token is None
    assert loaded.providers["second"].auth_type == "none"


def test_shared_identity_without_an_active_match_is_rejected(codex_home):
    first = gateway().model_copy(update={"id": "first", "codex_provider_id": "shared"})
    second = gateway().model_copy(update={"id": "second", "codex_provider_id": "shared"})
    original = (
        CmgrSettings(active_provider_id="unmatched", providers={"first": first, "second": second})
        .model_dump_json()
        .encode()
    )
    (codex_home / "cmgr.json").write_bytes(original)
    (codex_home / "config.toml").write_text('model_provider="shared"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="share"):
        load_settings()
    assert (codex_home / "cmgr.json").read_bytes() == original


def test_imported_headers_and_transport_replace_stale_manager_credentials(codex_home):
    save_settings(CmgrSettings(active_provider_id="gateway", providers={"gateway": gateway()}))
    (codex_home / "config.toml").write_text(
        'model="native"\nmodel_provider="gateway"\n[model_providers.gateway]\nname="Header gateway"\nbase_url="https://fixture.invalid/v1"\nwire_api="chat"\n[model_providers.gateway.env_http_headers]\nX-API-Key="SYNTHETIC_HEADER_KEY"\n',
        encoding="utf-8",
    )
    loaded = load_settings().providers["gateway"]
    assert loaded.auth_token is None and loaded.wire_api == "chat"
    assert loaded.auth_type == "bearer_token"
    assert loaded.env_http_headers == {"X-API-Key": "SYNTHETIC_HEADER_KEY"}


def test_header_only_native_credentials_are_emitted_without_extra_key(codex_home):
    provider = gateway().model_copy(
        update={
            "auth_type": "bearer_token",
            "auth_token": None,
            "env_http_headers": {"X-API-Key": "SYNTHETIC_HEADER_KEY"},
        }
    )
    assert switch_active_provider(provider)[0]
    table = read_config_toml()["model_providers"]["gateway"]
    assert table["env_http_headers"] == {"X-API-Key": "SYNTHETIC_HEADER_KEY"}
    assert "env_key" not in table and "experimental_bearer_token" not in table


@pytest.mark.parametrize(
    "patch",
    [
        {"version": 999},
        {"preferences": {"unknown_future_preference": "private-value"}},
        {"preferences": {"proxy_port": 10}},
    ],
)
def test_unknown_settings_version_or_preference_is_not_silently_discarded(codex_home, patch):
    raw = json.dumps({"providers": {}, **patch}).encode()
    (codex_home / "cmgr.json").write_bytes(raw)
    with pytest.raises(ValueError, match="settings"):
        load_settings()
    assert (codex_home / "cmgr.json").read_bytes() == raw


@pytest.mark.parametrize(
    "value",
    ['env_key=["invalid"]', 'wire_api="unknown-transport"', 'base_url="private-invalid-url"'],
)
def test_invalid_imported_connection_has_generic_error_and_preserves_files(codex_home, value):
    raw = (
        'model_provider="fixture"\n[model_providers.fixture]\nname="Fixture"\n'
        + ("" if value.startswith("base_url") else 'base_url="https://fixture.invalid/v1"\n')
        + value
        + "\n"
    ).encode()
    (codex_home / "config.toml").write_bytes(raw)
    with pytest.raises(ValueError) as error:
        load_settings()
    assert "private-invalid" not in str(error.value)
    assert (codex_home / "config.toml").read_bytes() == raw
    assert not (codex_home / "cmgr.json").exists()


def test_imported_connection_without_model_requires_explicit_selection(codex_home):
    (codex_home / "config.toml").write_text(
        'model_provider="fixture"\n[model_providers.fixture]\nname="Fixture"\nbase_url="https://fixture.invalid/v1"\nwire_api="responses"\n',
        encoding="utf-8",
    )
    provider = load_settings().providers["fixture"]
    assert provider.model_selected is False
    assert switch_active_provider(provider)[0] is False


@pytest.mark.parametrize("variable", ["$KEY", "INVALID VARIABLE", "", "KEY=private-value"])
def test_environment_header_names_are_validated(variable):
    with pytest.raises(ValueError):
        gateway(env_http_headers={"X-API-Key": variable})
