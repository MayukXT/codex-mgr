"""Opt-in native parser/writer contract; synthetic home and no inference."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from codex_mgr.config.schema import CmgrSettings, ProviderConfig
from codex_mgr.config.settings import save_settings
from codex_mgr.core.activation import apply_settings
from codex_mgr.core.bridge import bridge_session_rollout
from codex_mgr.core.native_config import read_native_configuration
from codex_mgr.core.rpc import CodexReader
from codex_mgr.core.session_lock import session_write_guard
from codex_mgr.utils.files import FileConflictError
from codex_mgr.utils.paths import find_codex_binary, get_codex_home


def test_native_activation_verifies_effective_connection_and_controls(tmp_path, monkeypatch):
    binary = find_codex_binary()
    assert binary is not None, "Install Codex 0.159.0 to run this contract."
    version = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True, timeout=10
    )
    assert version.stdout.strip().endswith(" 0.159.0")
    monkeypatch.chdir(tmp_path)
    home = get_codex_home()
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        'model="synthetic-original"\ncli_auth_credentials_store="file"\n', encoding="utf-8"
    )
    provider = ProviderConfig(
        id="fixture",
        name="Fixture",
        base_url="http://127.0.0.1:1/v1",
        default_model="synthetic-selected",
        auth_type="none",
        reasoning_effort="high",
        context_window_limit=8192,
        http_headers={"X-Synthetic": "fixture"},
    )
    settings = CmgrSettings(providers={"fixture": provider})
    save_settings(settings)
    ok, message = apply_settings(settings, activate_provider_id="fixture")
    assert ok, message
    view = read_native_configuration(cwd=tmp_path)
    assert view.config["model"] == "synthetic-selected"
    assert view.config["model_provider"] == "fixture"
    assert view.config["model_reasoning_effort"] == "high"
    assert view.config["model_context_window"] == 8192
    connection = view.config["model_providers"]["fixture"]
    assert connection["base_url"] == "http://127.0.0.1:1/v1"
    assert connection["wire_api"] == "responses" and connection["requires_openai_auth"] is False
    assert connection["http_headers"] == {"X-Synthetic": "fixture"}


def test_native_project_model_conflict_preserves_saved_selection(tmp_path, monkeypatch):
    import tomlkit

    binary = find_codex_binary()
    assert binary is not None
    monkeypatch.chdir(tmp_path)
    home = get_codex_home()
    home.mkdir(parents=True)
    config = {
        "model": "synthetic-original",
        "cli_auth_credentials_store": "file",
        "projects": {str(tmp_path): {"trust_level": "trusted"}},
    }
    path = home / "config.toml"
    path.write_text(tomlkit.dumps(config), encoding="utf-8")
    project = tmp_path / ".codex"
    project.mkdir()
    (project / "config.toml").write_text('model="synthetic-project"\n', encoding="utf-8")
    assert read_native_configuration().config["model"] == "synthetic-project"
    provider = ProviderConfig(
        id="fixture",
        name="Fixture",
        base_url="http://127.0.0.1:1/v1",
        default_model="synthetic-selected",
        auth_type="none",
    )
    settings = CmgrSettings(providers={"fixture": provider})
    save_settings(settings)
    before = path.read_bytes()
    saved = (home / "cmgr.json").read_bytes()
    ok, message = apply_settings(settings, activate_provider_id="fixture")
    assert not ok and "layer" in message
    assert path.read_bytes() == before and (home / "cmgr.json").read_bytes() == saved
    assert not (home / "backups/config").exists()


def test_native_automatic_controls_inherit_trusted_project_values(tmp_path, monkeypatch):
    import tomlkit

    assert find_codex_binary() is not None
    monkeypatch.chdir(tmp_path)
    home = get_codex_home()
    home.mkdir(parents=True)
    path = home / "config.toml"
    path.write_text(
        tomlkit.dumps(
            {
                "cli_auth_credentials_store": "file",
                "projects": {str(tmp_path): {"trust_level": "trusted"}},
            }
        ),
        encoding="utf-8",
    )
    project = tmp_path / ".codex"
    project.mkdir()
    (project / "config.toml").write_text(
        'model_reasoning_effort="low"\nmodel_context_window=4096\n', encoding="utf-8"
    )
    connection = ProviderConfig(
        id="fixture",
        name="Fixture",
        base_url="http://127.0.0.1:1/v1",
        default_model="synthetic-selected",
        auth_type="none",
    )
    settings = CmgrSettings(providers={"fixture": connection})
    save_settings(settings)
    ok, message = apply_settings(settings, activate_provider_id="fixture")
    assert ok, message
    effective = read_native_configuration(cwd=tmp_path).config
    assert effective["model"] == "synthetic-selected"
    assert effective["model_provider"] == "fixture"
    assert effective["model_reasoning_effort"] == "low"
    assert effective["model_context_window"] == 4096
    saved = tomlkit.parse(path.read_text(encoding="utf-8"))
    assert "model_reasoning_effort" not in saved and "model_context_window" not in saved


def test_native_revision_repair_verifies_real_restored_config(tmp_path, monkeypatch):
    from codex_mgr.config.settings import serialize_settings
    from codex_mgr.core.profiles import prepare_configuration
    from codex_mgr.core.revisions import apply_revision_restore, prepare_revision_restore
    from codex_mgr.utils.files import create_snapshot, read_snapshot
    from codex_mgr.utils.paths import get_cmgr_settings_path, get_config_toml_path

    assert find_codex_binary() is not None
    monkeypatch.chdir(tmp_path)
    home = get_codex_home()
    home.mkdir(parents=True)
    selected = ProviderConfig(
        id="fixture",
        name="Fixture",
        base_url="http://127.0.0.1:1/v1",
        default_model="synthetic-restored",
        auth_type="none",
    )
    settings = CmgrSettings(active_provider_id="fixture", providers={"fixture": selected})
    raw = prepare_configuration(
        selected, b'# retained comment\ncli_auth_credentials_store="file"\n'
    )
    get_config_toml_path().write_bytes(raw)
    get_cmgr_settings_path().write_bytes(serialize_settings(settings))
    config_revision = create_snapshot(
        get_config_toml_path(), home / "backups/config", prefix="config_", suffix=".toml"
    )
    settings_revision = create_snapshot(
        get_cmgr_settings_path(), home / "backups/settings", prefix="cmgr_", suffix=".json"
    )
    get_config_toml_path().write_bytes(b"malformed TOML")
    get_cmgr_settings_path().write_bytes(b"malformed JSON")
    plan = prepare_revision_restore(
        config=config_revision.name, settings=settings_revision.name, repair=True
    )
    ok, message = apply_revision_restore(plan)
    assert ok, message
    assert get_config_toml_path().read_bytes() == raw
    actual = read_native_configuration().config
    assert actual["model"] == "synthetic-restored" and actual["model_provider"] == "fixture"
    assert actual["model_providers"]["fixture"]["base_url"] == "http://127.0.0.1:1/v1"
    assert b"malformed TOML" in [
        read_snapshot(path) for path in (home / "backups/config").glob("config_*.toml")
    ]
    assert b"malformed JSON" in [
        read_snapshot(path) for path in (home / "backups/settings").glob("cmgr_*.json")
    ]


def test_native_revision_rejects_reserved_builtin_override(tmp_path, monkeypatch):
    from codex_mgr.core.profiles import ConfigurationError
    from codex_mgr.core.revisions import prepare_revision_restore
    from codex_mgr.utils.files import create_snapshot
    from codex_mgr.utils.paths import get_config_toml_path

    assert find_codex_binary() is not None
    monkeypatch.chdir(tmp_path)
    home = get_codex_home()
    home.mkdir(parents=True)
    raw = b'model="synthetic-selected"\nmodel_provider="openai"\nforced_login_method="api"\ncli_auth_credentials_store="file"\n[model_providers.openai]\nname="Synthetic API"\nbase_url="http://127.0.0.1:1/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
    get_config_toml_path().write_bytes(raw)
    saved = create_snapshot(
        get_config_toml_path(), home / "backups/config", prefix="config_", suffix=".toml"
    )
    with pytest.raises(ConfigurationError, match="could not verify"):
        read_native_configuration()
    current = b'model="synthetic-current"\ncli_auth_credentials_store="file"\n'
    get_config_toml_path().write_bytes(current)
    with pytest.raises(ConfigurationError, match="reserves"):
        prepare_revision_restore(config=saved.name)
    assert get_config_toml_path().read_bytes() == current and saved.read_bytes() == raw
    valid = create_snapshot(
        get_config_toml_path(), home / "backups/config", prefix="config_", suffix=".toml"
    )
    get_config_toml_path().write_bytes(raw)
    from codex_mgr.core.revisions import apply_revision_restore

    plan = prepare_revision_restore(config=valid.name)
    assert not apply_revision_restore(plan)[0]
    assert get_config_toml_path().read_bytes() == raw
    repaired = prepare_revision_restore(config=valid.name, repair=True)
    ok, message = apply_revision_restore(repaired)
    assert ok, message
    assert get_config_toml_path().read_bytes() == current
    assert read_native_configuration().config["model"] == "synthetic-current"


pytestmark = pytest.mark.skipif(
    os.environ.get("CMGR_NATIVE_CONTRACTS") != "1",
    reason="Set CMGR_NATIVE_CONTRACTS=1 with Codex 0.159.0 installed.",
)


class SyntheticWriter(CodexReader):
    """Test-only writer; CODEX_HOME is isolated by the autouse test fixture."""

    ALLOWED = CodexReader.ALLOWED | {"thread/resume"}


def test_native_converted_session_and_writer_coordination(tmp_path, monkeypatch):
    Path(os.environ["CODEX_SQLITE_HOME"]).resolve().relative_to(tmp_path.resolve())
    binary = find_codex_binary()
    assert binary is not None, "Install Codex 0.159.0 to run this contract."
    version = subprocess.run(
        [str(binary), "--version"], capture_output=True, text=True, check=True, timeout=10
    )
    assert version.stdout.strip().endswith(" 0.159.0"), "This contract is pinned to Codex 0.159.0."
    monkeypatch.chdir(tmp_path)
    home = get_codex_home()
    home.mkdir(parents=True)
    (home / "config.toml").write_text(
        'model="synthetic-model"\nmodel_provider="fixture"\ncli_auth_credentials_store="file"\n[model_providers.fixture]\nname="Fixture"\nbase_url="http://127.0.0.1:1/v1"\nwire_api="responses"\nrequires_openai_auth=false\n',
        encoding="utf-8",
    )
    sid = "00000000-0000-0000-0000-000000000001"
    folder = home / "sessions" / "2026" / "10" / "10"
    folder.mkdir(parents=True)
    path = folder / f"rollout-2026-10-10T12-00-00-{sid}.jsonl"
    records = [
        {
            "type": "session_meta",
            "payload": {
                "id": sid,
                "timestamp": "2026-10-10T12:00:00Z",
                "cwd": str(home),
                "originator": "synthetic-check",
                "cli_version": "0.159.0",
                "source": "cli",
                "model_provider": "old",
                "history_mode": "legacy",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Synthetic user prompt"}],
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "rs_synthetic",
                "summary": [{"type": "summary_text", "text": "Synthetic visible summary"}],
                "encrypted_content": "opaque-synthetic",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "synthetic_tool",
                "arguments": "{}",
                "call_id": "call_synthetic",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "Synthetic tool result",
                "call_id": "call_synthetic",
            },
        },
        {
            "type": "compacted",
            "payload": {
                "message": "Synthetic summary",
                "replacement_history": [
                    {
                        "type": "reasoning",
                        "id": "rs_nested",
                        "summary": [],
                        "encrypted_content": "opaque-nested",
                    },
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Synthetic preserved context"}],
                    },
                ],
                "replacement_history_metadata": [{}, {}],
            },
        },
    ]
    path.write_text(
        "\n".join(json.dumps({"timestamp": "2026-10-10T12:00:00Z", **record}) for record in records)
        + "\n",
        encoding="utf-8",
    )
    original = path.read_bytes()
    ok, count, _ = bridge_session_rollout(path, "fixture")
    assert ok and count == 2
    with CodexReader(timeout=12) as reader:
        thread = reader.request("thread/read", {"threadId": sid, "includeTurns": True})["thread"]
        assert thread["id"] == sid and thread["modelProvider"] == "fixture"
    converted = path.read_bytes()
    assert converted != original
    with SyntheticWriter(timeout=12) as writer:
        assert writer.request("thread/resume", {"threadId": sid})["thread"]["id"] == sid
        with pytest.raises(FileConflictError), session_write_guard(sid, path):
            pytest.fail("Native writer ownership must block manager mutation.")
