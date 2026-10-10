"""Reasoning envelopes preserve exact blocks and reject wrong identities/keys."""

import base64
import os

import pytest
from test_protocol_codecs import messages_reply, request
from test_streaming_codecs import assert_lifecycle, block, finish, start, stop, wire

from codex_mgr.core.adapters.continuations import (
    ContinuationCodec,
    connection_scope,
    load_continuation_key,
)
from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.responses import adapt_response
from codex_mgr.core.adapters.streaming import adapt_stream, buffered_stream
from codex_mgr.core.adapters.tools import ProtocolError
from codex_mgr.utils.paths import get_codex_home

SIGNED = {
    "type": "thinking",
    "thinking": "  Exact synthetic 🌙\n",
    "signature": "synthetic-signature",
}
REDACTED = {"type": "redacted_thinking", "data": "synthetic-opaque-data"}


def codec(
    *,
    endpoint="https://synthetic.invalid/v1/messages",
    credential="synthetic-credential",
    key=b"k" * 32,
):
    return ContinuationCodec(
        connection_scope("messages", endpoint, {"X-Api-Key": credential}), lambda _: key
    )


@pytest.mark.parametrize("block", [SIGNED, REDACTED])
def test_exact_block_round_trip_uses_distinct_nonces_and_hides_plaintext(block):
    owner = codec()
    first, second = owner.seal(block, "synthetic-model"), owner.seal(block, "synthetic-model")
    assert first != second and owner.open(first, "synthetic-model") == block
    assert owner.open(second, "synthetic-model") == block
    assert all(value not in first for value in block.values())
    assert "synthetic-credential" not in repr(owner) and "kkkk" not in repr(owner)


@pytest.mark.parametrize(
    "changed", ["endpoint", "credential", "model", "key", "scope", "nonce", "cipher"]
)
def test_cross_connection_model_key_and_tampering_are_rejected(changed):
    owner, model = codec(), "synthetic-model"
    token = owner.seal(SIGNED, model)
    if changed in {"endpoint", "credential", "key"}:
        owner = codec(
            **{changed: b"z" * 32 if changed == "key" else "https://other.invalid/messages"}
        )
    elif changed == "model":
        model = "other-model"
    elif changed == "scope":
        token = token.replace(token.split(".")[1], "0" * 64)
    else:
        prefix, scope, encoded = token.split(".")
        raw = bytearray(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        raw[0 if changed == "nonce" else -1] ^= 1
        token = ".".join([prefix, scope, base64.urlsafe_b64encode(raw).decode().rstrip("=")])
    with pytest.raises(ProtocolError) as failure:
        owner.open(token, model)
    assert "synthetic" not in str(failure.value)


def test_header_case_and_order_preserve_binding_but_duplicate_names_fail():
    assert connection_scope("messages", "synthetic", {"X-Key": "a", "Y": "b"}) == connection_scope(
        "messages", "synthetic", {"y": "b", "x-key": "a"}
    )
    with pytest.raises(ProtocolError):
        connection_scope("messages", "synthetic", {"X-Key": "a", "x-key": "a"})


@pytest.mark.parametrize(
    "block",
    [
        {},
        {"type": "thinking", "thinking": "text"},
        {**SIGNED, "extra": True},
        {**SIGNED, "signature": ""},
        {**REDACTED, "data": None},
    ],
)
def test_invalid_blocks_do_not_create_key_storage(block):
    owner = ContinuationCodec(connection_scope("messages", "synthetic", {}))
    with pytest.raises(ProtocolError):
        owner.seal(block, "model")
    assert not get_codex_home().exists()


def test_key_is_durable_restrictive_and_lost_or_corrupt_key_is_not_replaced():
    home = get_codex_home()
    with pytest.raises(ProtocolError):
        load_continuation_key(False)
    assert not home.exists()
    first = load_continuation_key(True)
    assert len(first) == 32 and load_continuation_key(False) == first
    path = home / "cmgr-adapter-continuations.key"
    if os.name != "nt":
        assert path.stat().st_mode & 0o077 == 0
    path.write_bytes(b"corrupt")
    with pytest.raises(ProtocolError):
        load_continuation_key(True)
    assert path.read_bytes() == b"corrupt"


@pytest.mark.parametrize(
    "envelope",
    ["unknown", "cmgr1." + "0" * 64 + ".!", "x" * (3 * 1024 * 1024 + 1)],
    ids=["unknown", "invalid-encoding", "oversized"],
)
def test_malformed_envelopes_fail_before_key_creation(envelope):
    owner = ContinuationCodec(connection_scope("messages", "synthetic", {}))
    with pytest.raises(ProtocolError):
        owner.open(envelope, "model")
    assert not get_codex_home().exists()


def adapted(owner=None, **kwargs):
    return adapt_request(
        request(reasoning={"effort": "medium"}, **kwargs),
        "messages",
        thinking_budget=1024,
        continuations=owner or codec(),
    )


@pytest.mark.parametrize("block_value", [SIGNED, REDACTED])
def test_buffered_thinking_reconstructs_exact_provider_history(block_value):
    original = adapted()
    body = messages_reply([block_value, {"type": "text", "text": "Exact response."}])
    response = adapt_response(body, original)
    assert response["output"][0]["type"] == "reasoning"
    assert response["output"][0]["summary"] == []
    replay = adapted(input=[*response["output"], {"role": "user", "content": "Continue."}])
    assert replay.payload["messages"][0]["content"][0] == block_value
    assert replay.payload["messages"][-1]["content"] == [{"type": "text", "text": "Continue."}]
    events = list(buffered_stream(body, original))
    assert_lifecycle(events)
    assert events[-1]["type"] == "response.completed"


def thinking_stream():
    return [
        start(),
        block(0, {"type": "thinking", "thinking": "", "signature": ""}),
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "thinking_delta", "thinking": SIGNED["thinking"]},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "signature_delta", "signature": SIGNED["signature"]},
        },
        stop(0),
        block(1, REDACTED),
        stop(1),
        block(2, {"type": "text", "text": "Exact response."}),
        stop(2),
        finish(),
        {"type": "message_stop"},
    ]


@pytest.mark.parametrize("chunk_size", [1, 11, None])
def test_streamed_signed_and_redacted_blocks_preserve_order_and_exact_history(chunk_size):
    original = adapted()
    events = list(
        adapt_stream(wire(thinking_stream(), "messages", chunk_size=chunk_size), original)
    )
    assert_lifecycle(events)
    assert events[-1]["type"] == "response.completed"
    output = events[-1]["response"]["output"]
    assert [item["type"] for item in output] == ["reasoning", "reasoning", "message"]
    replay = adapted(input=[*output, {"role": "user", "content": "Continue."}])
    assert replay.payload["messages"][0]["content"][:2] == [SIGNED, REDACTED]
    assert SIGNED["thinking"] not in str(events) and SIGNED["signature"] not in str(events)


@pytest.mark.parametrize(
    "failure",
    ["signature-missing", "text-after-signature", "trailing-error", "disabled", "unbound"],
)
def test_invalid_or_disabled_thinking_never_publishes_continuations(failure):
    source, original = thinking_stream(), adapted()
    if failure == "signature-missing":
        del source[3]
    elif failure == "text-after-signature":
        source.insert(4, source[2])
    elif failure == "trailing-error":
        source.append({"type": "error", "error": {"message": "synthetic-private"}})
    elif failure == "disabled":
        original = adapt_request(
            request(reasoning={"effort": "none"}), "messages", continuations=codec()
        )
    else:
        original = adapt_request(
            request(reasoning={"effort": "medium"}), "messages", thinking_budget=1024
        )
    events = list(adapt_stream(wire(source, "messages"), original))
    assert events[-1]["type"] == "response.failed"
    assert not any(event["type"] == "response.output_item.done" for event in events)


def test_missing_key_and_changed_identity_stop_history_before_any_rewrite():
    owner = ContinuationCodec(connection_scope("messages", "synthetic", {}))
    token = owner.seal(SIGNED, "synthetic-model")
    path = get_codex_home() / "cmgr-adapter-continuations.key"
    path.unlink()
    with pytest.raises(ProtocolError):
        owner.open(token, "synthetic-model")
    with pytest.raises(ProtocolError):
        load_continuation_key(True)
    assert not path.exists()


@pytest.mark.parametrize("fault", ["modified-key", "missing-record", "modified-record"])
def test_damaged_key_pair_is_preserved_and_cannot_encrypt_new_history(fault):
    load_continuation_key(True)
    path = get_codex_home() / "cmgr-adapter-continuations.key"
    checksum = path.with_name(path.name + ".sha256")
    if fault == "modified-key":
        path.write_bytes(b"z" * 32)
    elif fault == "missing-record":
        checksum.unlink()
    else:
        checksum.write_bytes(b"0" * 64 + b"\n")
    before = path.read_bytes()
    with pytest.raises(ProtocolError):
        load_continuation_key(True)
    assert path.read_bytes() == before
    if fault == "missing-record":
        assert not checksum.exists()


def test_linked_key_or_lock_storage_preserves_existing_files(tmp_path):
    home = get_codex_home()
    home.mkdir(parents=True)
    original = tmp_path / "original-key"
    original.write_bytes(b"k" * 32)
    path = home / "cmgr-adapter-continuations.key"
    os.link(original, path)
    with pytest.raises(ProtocolError):
        load_continuation_key(True)
    assert original.read_bytes() == b"k" * 32
    path.unlink()
    os.link(original, path.with_name(path.name + ".cmgr.lock"))
    with pytest.raises(ProtocolError):
        load_continuation_key(True)
    assert original.read_bytes() == b"k" * 32 and not path.exists()
