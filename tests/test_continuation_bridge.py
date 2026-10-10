"""Preview continuation loss against the destination, independent of its label."""

import json

import pytest
from test_bridge import SID, read_records, session
from test_continuations import SIGNED
from test_protocol_codecs import request

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.core import models
from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.runtime import AdapterRuntime
from codex_mgr.core.adapters.tools import ProtocolError
from codex_mgr.core.auth import CredentialError
from codex_mgr.core.backup import list_session_backups
from codex_mgr.core.bridge import BridgeError, prepare_bridge
from codex_mgr.utils.files import read_snapshot
from codex_mgr.utils.paths import get_codex_home


def provider(**changes):
    values = {
        "id": "synthetic",
        "name": "Synthetic",
        "base_url": "https://synthetic.invalid/v1",
        "wire_api": "anthropic_proxy",
        "default_model": "synthetic-model",
        "auth_type": "none",
    }
    values.update(changes)
    return ProviderConfig(**values)


def signed_item(runtime):
    return {
        "type": "reasoning",
        "id": "rs_synthetic",
        "summary": [{"type": "summary_text", "text": "  Exact summary 🌙\n"}],
        "encrypted_content": runtime._continuations.seal(SIGNED, "synthetic-model"),
    }


@pytest.mark.parametrize("target", ["same", "renamed"])
def test_compatible_capsule_and_all_nonconverted_bytes_survive(target, tmp_path):
    runtime = AdapterRuntime(provider())
    item = signed_item(runtime)
    path = session(tmp_path, {"type": "response_item", "payload": item}, provider="same")
    original = path.read_bytes()
    with prepare_bridge(path, target, preserve_reasoning=runtime.reasoning_compatible) as prepared:
        assert prepared.sanitized_count == 0
        assert prepared.changed_records == (0 if target == "same" else 1)
        assert path.read_bytes() == original
        assert not list_session_backups(SID)
        prepared.commit()
    assert read_records(path)[1]["payload"] == item
    if target == "same":
        assert path.read_bytes() == original and not list_session_backups(SID)
    else:
        assert read_snapshot(list_session_backups(SID)[0]) == original


@pytest.mark.parametrize(
    "changed", ["endpoint", "credential", "model", "protocol", "key", "cipher"]
)
def test_foreign_same_id_capsule_is_staged_and_snapshotted_before_loss(changed, tmp_path):
    original_runtime = AdapterRuntime(provider())
    item = signed_item(original_runtime)
    selected = provider()
    if changed == "endpoint":
        selected.base_url = "https://other.invalid/v1"
    elif changed == "credential":
        selected.auth_type = "bearer_token"
        selected.auth_token = "synthetic-other-key"
    elif changed == "model":
        selected.default_model = "synthetic-other-model"
    elif changed == "protocol":
        selected.wire_api = "chat"
    elif changed == "key":
        (get_codex_home() / "cmgr-adapter-continuations.key").unlink()
    else:
        item["encrypted_content"] += "x"
    destination = AdapterRuntime(selected)
    path = session(tmp_path, {"type": "response_item", "payload": item}, provider="same")
    tail = b' {"type":"event_msg","payload":{"type":"user_message","message":"  exact user prompt  "},"decimal":0.123456789012345678901234567890}\r\n'
    path.write_bytes(path.read_bytes() + tail)
    original = path.read_bytes()
    with prepare_bridge(
        path, "same", preserve_reasoning=destination.reasoning_compatible
    ) as prepared:
        assert prepared.sanitized_count == 1 and prepared.changed_records == 1
        assert path.read_bytes() == original and not list_session_backups(SID)
        staged = read_records(prepared.temporary)[1]["payload"]
        assert staged["encrypted_content"] is None and staged["id"] is None
        assert staged["summary"] == item["summary"]
        prepared.commit()
    assert path.read_bytes().endswith(tail)
    assert read_snapshot(list_session_backups(SID)[0]) == original


def test_manager_version_change_keeps_binding_but_custom_user_agent_is_bound(monkeypatch):
    selected = provider()
    original = AdapterRuntime(selected)
    item = signed_item(original)
    monkeypatch.setattr(models, "__version__", "999.0.0")
    assert AdapterRuntime(selected).reasoning_compatible(item)
    custom = provider(http_headers={"User-Agent": "synthetic-custom-one"})
    custom_item = signed_item(AdapterRuntime(custom))
    custom.http_headers = {"USER-AGENT": "synthetic-custom-two"}
    assert not AdapterRuntime(custom).reasoning_compatible(custom_item)


@pytest.mark.parametrize(
    "record",
    [
        {
            "type": "response_item",
            "payload": {"type": "compaction", "encrypted_content": "synthetic"},
        },
        {"type": "response_item", "payload": {"type": "compaction", "encrypted_content": ""}},
        {
            "type": "response_item",
            "payload": {"type": "agent_message", "content": [{"type": "encrypted_content"}]},
        },
        {
            "type": "response_item",
            "payload": {"type": "function_call_output", "output": [{"type": "encrypted_content"}]},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": [{"type": "encrypted_content"}],
            },
        },
        {
            "type": "inter_agent_communication",
            "payload": {"content": "Exact readable content", "encrypted_content": "synthetic"},
        },
    ],
    ids=["compaction", "empty-compaction", "message", "function-result", "custom-result", "agent"],
)
def test_same_id_destination_check_rejects_unverified_opaque_data(record, tmp_path):
    path = session(tmp_path, record, provider="same")
    original = path.read_bytes()
    with (
        pytest.raises(BridgeError, match="encrypted"),
        prepare_bridge(
            path,
            "same",
            preserve_reasoning=AdapterRuntime(provider(wire_api="chat")).reasoning_compatible,
        ),
    ):
        pytest.fail("Opaque data must be checked before conversion can commit.")
    assert path.read_bytes() == original
    assert not list_session_backups(SID)
    assert not list(tmp_path.glob("*.bridge.tmp"))


def test_no_auth_ignores_leftover_token_without_resolving_or_sending_it(monkeypatch):
    monkeypatch.delenv("SYNTHETIC_UNSET", raising=False)
    selected = provider(auth_token="$SYNTHETIC_UNSET")
    assert "x-api-key" not in models.provider_headers(selected)
    selected.auth_token = "synthetic-leftover-key"
    assert "x-api-key" not in models.provider_headers(selected)
    selected.http_headers = {"X-Explicit": "synthetic-explicit-header"}
    assert models.provider_headers(selected)["X-Explicit"] == "synthetic-explicit-header"
    assert not get_codex_home().exists()


def test_header_overrides_merge_by_http_name_and_explicit_environment_priority(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_HEADER", "synthetic-environment")
    selected = provider(
        auth_type="bearer_token",
        auth_token="synthetic-implicit-key",
        http_headers={"user-agent": "synthetic-agent", "X-Key": "synthetic-literal"},
        env_http_headers={"x-key": "SYNTHETIC_HEADER"},
    )
    headers = models.provider_headers(selected)
    assert len(headers) == len({name.lower() for name in headers})
    assert headers["user-agent"] == "synthetic-agent"
    assert headers["x-key"] == "synthetic-environment" and "X-Key" not in headers


@pytest.mark.parametrize("field", ["http_headers", "env_http_headers"])
def test_duplicate_configured_header_names_reject_without_secret_diagnostics(field):
    selected = provider(**{field: {"X-Synthetic": "SYNTHETIC", "x-synthetic": "OTHER"}})
    with pytest.raises(CredentialError, match="ambiguous") as error:
        models.provider_headers(selected)
    assert "SYNTHETIC" not in str(error.value) and "OTHER" not in str(error.value)


def test_same_connection_readable_reasoning_id_is_not_rewritten(tmp_path):
    item = {"type": "reasoning", "id": "rs_synthetic", "summary": [], "encrypted_content": None}
    path = session(tmp_path, {"type": "response_item", "payload": item}, provider="same")
    before = path.read_bytes()
    with prepare_bridge(path, "same", preserve_reasoning=lambda _: False) as prepared:
        assert prepared.changed_records == prepared.sanitized_count == 0
        prepared.commit()
    assert path.read_bytes() == before


def test_explicit_strip_overrides_capsule_preservation(tmp_path):
    runtime = AdapterRuntime(provider())
    item = signed_item(runtime)
    path = session(tmp_path, {"type": "response_item", "payload": item}, provider="same")
    with prepare_bridge(
        path, "same", strip_reasoning=True, preserve_reasoning=runtime.reasoning_compatible
    ) as prepared:
        assert prepared.sanitized_count == 1


def test_canonical_inherited_store_is_not_rewritten_by_file_policy(tmp_path):
    path = session(tmp_path, provider="same", metadata={"history_mode": "paginated"})
    original = path.read_bytes()
    with (
        pytest.raises(BridgeError, match="canonical"),
        prepare_bridge(path, "same", preserve_reasoning=lambda _: True),
    ):
        pytest.fail("Canonical storage requires a separate workflow.")
    assert path.read_bytes() == original and not list_session_backups(SID)


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize(
    "summary", [[], [{"type": "summary_text", "text": "  Exact summary 🌙\n"}]]
)
def test_readable_reasoning_is_exact_context_and_empty_removed_items_are_skipped(protocol, summary):
    item = {"type": "reasoning", "id": None, "summary": summary, "encrypted_content": None}
    original = json.dumps(item)
    adapted = adapt_request(
        request(input=[item, {"role": "user", "content": "Continue."}]), protocol
    )
    messages = adapted.payload["messages"]
    if summary:
        assert messages[0] == {
            "role": "assistant",
            "content": [{"type": "text", "text": summary[0]["text"]}],
        }
    else:
        assert len(messages) == 1 and messages[0]["role"] == "user"
    assert json.dumps(item) == original


@pytest.mark.parametrize(
    "changes",
    [
        {"content": [{"type": "reasoning_text", "text": "synthetic"}]},
        {"summary": ["invalid"]},
        {"summary": [{"type": "unknown", "text": "synthetic"}]},
        {"encrypted_content": ""},
        {"status": "incomplete"},
    ],
)
def test_unverified_readable_or_empty_cipher_items_reject(changes):
    item = {"type": "reasoning", "summary": [], "encrypted_content": None, **changes}
    with pytest.raises(ProtocolError):
        adapt_request(request(input=[item, {"role": "user", "content": "Continue."}]), "chat")
