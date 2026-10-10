"""Reasoning envelopes preserve exact blocks and reject wrong identities/keys."""

import asyncio
import base64
import os
from unittest.mock import Mock

import pytest
from test_protocol_codecs import FUNCTION, messages_reply, request
from test_streaming_codecs import assert_lifecycle, block, finish, start, stop, wire

from codex_mgr.core.adapters.continuations import (
    ContinuationCodec,
    connection_scope,
    load_continuation_key,
)
from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.responses import adapt_response
from codex_mgr.core.adapters.streaming import adapt_stream, async_adapt_stream, buffered_stream
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
    replay = adapted(
        input=[
            {"role": "user", "content": request()["input"]},
            *response["output"],
            {"role": "user", "content": "Continue."},
        ]
    )
    assert replay.payload["messages"][1]["content"][0] == block_value
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


def summarized(owner=None, *, summary="auto", **kwargs):
    return adapt_request(
        request(reasoning={"effort": "medium", "summary": summary}, **kwargs),
        "messages",
        thinking_budget=1024,
        continuations=owner or codec(),
    )


@pytest.mark.parametrize("summary", ["auto", "none"])
@pytest.mark.parametrize("value", [SIGNED, {**SIGNED, "thinking": ""}, REDACTED])
def test_supplied_summary_is_opt_in_exact_and_does_not_alter_replay(summary, value):
    selected = summarized(summary=summary)
    assert selected.payload["thinking"]["display"] == (
        "summarized" if summary == "auto" else "omitted"
    )
    body = messages_reply([value, {"type": "text", "text": "Exact reply."}])
    response = adapt_response(body, selected)
    item = response["output"][0]
    expected = (
        [{"type": "summary_text", "text": value["thinking"]}]
        if summary == "auto" and value.get("thinking")
        else []
    )
    assert item["summary"] == expected
    replay = summarized(
        input=[
            {"role": "user", "content": request()["input"]},
            *response["output"],
            {"role": "user", "content": "Continue."},
        ]
    )
    assert replay.payload["messages"][1]["content"][0] == value
    assert value.get("signature", value.get("data")) not in str(expected)


def assert_summary_lifecycle(events, expected):
    assert_lifecycle(events)
    summary_events = [event for event in events if ".reasoning_summary_" in event["type"]]
    if not expected:
        assert summary_events == []
        return
    assert [event["type"] for event in summary_events] == [
        "response.reasoning_summary_part.added",
        "response.reasoning_summary_text.delta",
        "response.reasoning_summary_text.done",
        "response.reasoning_summary_part.done",
    ]
    assert summary_events[0]["part"] == {"type": "summary_text", "text": ""}
    assert summary_events[1]["delta"] == expected
    assert summary_events[2]["text"] == expected
    assert summary_events[3]["part"] == {"type": "summary_text", "text": expected}
    done = next(
        event
        for event in events
        if event["type"] == "response.output_item.done" and event["item"]["type"] == "reasoning"
    )
    assert all(event["item_id"] == done["item"]["id"] for event in summary_events)
    assert all(
        event["output_index"] == 0 and event["summary_index"] == 0 for event in summary_events
    )
    assert summary_events[-1]["sequence_number"] < done["sequence_number"]


@pytest.mark.parametrize("summary", ["auto", "none"])
@pytest.mark.parametrize("buffered", [False, True])
def test_validated_stream_summary_events_keep_exact_text_and_item_identity(summary, buffered):
    selected = summarized(summary=summary)
    body = messages_reply([SIGNED, REDACTED, {"type": "text", "text": "Exact response."}])
    events = list(
        buffered_stream(body, selected)
        if buffered
        else adapt_stream(wire(thinking_stream(), "messages", chunk_size=1), selected)
    )
    assert events[-1]["type"] == "response.completed"
    assert_summary_lifecycle(events, SIGNED["thinking"] if summary == "auto" else None)
    assert SIGNED["signature"] not in str(events) and REDACTED["data"] not in str(events)
    output = events[-1]["response"]["output"]
    assert selected.continuations.inspect(output[0]["encrypted_content"], selected.model) == SIGNED
    assert (
        selected.continuations.inspect(output[1]["encrypted_content"], selected.model) == REDACTED
    )


@pytest.mark.parametrize(
    "corruption", ["missing-signature", "truncated", "trailing", "transformed"]
)
def test_bad_stream_never_publishes_readable_summaries(corruption):
    source = thinking_stream()
    if corruption == "missing-signature":
        source[3]["delta"]["signature"] = ""
    elif corruption == "truncated":
        source.pop()
    elif corruption == "trailing":
        source.append(b"data: private-invalid-json\n\n")
    else:
        source[-2]["input_transformations"] = [{"reason": "synthetic-private"}]
    events = list(adapt_stream(wire(source, "messages"), summarized()))
    assert events[-1]["type"] == "response.failed"
    assert all(".reasoning_summary_" not in event["type"] for event in events)
    assert SIGNED["thinking"] not in str(events) and "synthetic-private" not in str(events)
    assert all(
        event["type"] != "response.output_item.done" or event["item"]["type"] != "reasoning"
        for event in events
    )


@pytest.mark.parametrize("corruption", ["missing-signature", "transformed", "late-invalid-block"])
def test_invalid_buffered_reply_never_returns_a_summary(corruption):
    body = messages_reply([dict(SIGNED), {"type": "text", "text": "Reply."}])
    if corruption == "missing-signature":
        body["content"][0]["signature"] = ""
    elif corruption == "transformed":
        body["input_transformations"] = [{"reason": "synthetic-private"}]
    else:
        body["content"].append({"type": "unsupported", "text": "synthetic-private"})
    with pytest.raises(ProtocolError) as failure:
        adapt_response(body, summarized())
    assert SIGNED["thinking"] not in str(failure.value) and "synthetic-private" not in str(
        failure.value
    )


@pytest.mark.parametrize("summary", ["auto", "none"])
def test_async_summary_delivery_waits_for_verified_eof(summary):
    async def run():
        async def chunks():
            for chunk in wire(thinking_stream(), "messages", chunk_size=11):
                yield chunk

        return [event async for event in async_adapt_stream(chunks(), summarized(summary=summary))]

    events = asyncio.run(run())
    assert events[-1]["type"] == "response.completed"
    assert_summary_lifecycle(events, SIGNED["thinking"] if summary == "auto" else None)


def test_stream_summary_is_withheld_until_upstream_eof():
    ended = []

    def chunks():
        yield from wire(thinking_stream(), "messages", chunk_size=11)
        ended.append(True)

    for event in adapt_stream(chunks(), summarized()):
        if ".reasoning_summary_" in event["type"] or event["type"] == "response.output_item.done":
            assert ended == [True]


def test_cancel_before_eof_cannot_publish_readable_reasoning():
    source = wire(thinking_stream(), "messages", chunk_size=11)
    iterator = adapt_stream(source, summarized())
    observed = []
    for event in iterator:
        observed.append(event)
        if event["type"] == "response.output_text.delta":
            break
    iterator.close()
    assert all(".reasoning_summary_" not in event["type"] for event in observed)
    assert SIGNED["thinking"] not in str(observed)


@pytest.mark.parametrize("protocol", ["messages", "chat"])
@pytest.mark.parametrize("summary", ["concise", "detailed"])
def test_summary_length_requests_are_not_silently_reinterpreted(protocol, summary):
    with pytest.raises(ProtocolError, match="summar"):
        adapt_request(
            request(reasoning={"effort": "medium", "summary": summary}),
            protocol,
            thinking_budget=1024 if protocol == "messages" else None,
        )


@pytest.mark.parametrize("protocol", ["messages", "chat"])
def test_summary_requests_require_supported_active_thinking(protocol):
    with pytest.raises(ProtocolError, match="summar"):
        adapt_request(request(reasoning={"effort": "none", "summary": "auto"}), protocol)


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
    replay = adapted(
        input=[
            {"role": "user", "content": request()["input"]},
            *output,
            {"role": "user", "content": "Continue."},
        ]
    )
    assert replay.payload["messages"][1]["content"][:2] == [SIGNED, REDACTED]
    assert SIGNED["thinking"] not in str(events) and SIGNED["signature"] not in str(events)


@pytest.mark.parametrize("report", [None, []])
def test_empty_transformation_reports_preserve_buffered_and_streamed_continuations(report):
    original = adapted()
    body = messages_reply([SIGNED, {"type": "text", "text": "Exact response."}])
    body["input_transformations"] = report
    response = adapt_response(body, original)
    assert (
        original.continuations.inspect(response["output"][0]["encrypted_content"], original.model)
        == SIGNED
    )
    source = thinking_stream()
    source[0]["message"]["input_transformations"] = report
    source[-2]["input_transformations"] = report
    events = list(adapt_stream(wire(source, "messages", chunk_size=11), original))
    assert_lifecycle(events)
    assert events[-1]["type"] == "response.completed"
    output = events[-1]["response"]["output"]
    assert adapted(
        input=[
            {"role": "user", "content": request()["input"]},
            *output,
            {"role": "user", "content": "Continue."},
        ]
    ).payload["messages"][1]["content"][:2] == [SIGNED, REDACTED]


_TRANSFORMATIONS = [
    [
        {
            "type": "thinking_dropped",
            "path": "synthetic-private-path",
            "reason": "prefix_binding_mismatch",
        }
    ],
    [
        {
            "type": "thinking_dropped",
            "path": "synthetic-private-path",
            "reason": "model_binding_mismatch",
        }
    ],
    [
        {
            "type": "thinking_dropped",
            "path": "synthetic-private-path",
            "reason": "organization_binding_mismatch",
        }
    ],
    [
        {
            "type": "thinking_mismatch_allowed",
            "path": "synthetic-private-path",
            "reason": "prefix_binding_mismatch",
        }
    ],
    [{"type": "unknown-future-transformation"}],
    False,
    {},
    "synthetic-private-report",
]


@pytest.mark.parametrize("report", _TRANSFORMATIONS)
def test_buffered_transformation_report_rejects_before_sealing_or_tool_completion(report):
    owner = Mock()
    original = adapted(owner)
    body = messages_reply([SIGNED, {"type": "text", "text": "Exact response."}])
    body["input_transformations"] = report
    with pytest.raises(ProtocolError) as failure:
        adapt_response(body, original)
    assert "synthetic-private" not in str(failure.value)
    owner.seal.assert_not_called()
    assert not get_codex_home().exists()


@pytest.mark.parametrize("position", ["start", "fallback"])
@pytest.mark.parametrize("report", _TRANSFORMATIONS)
def test_streamed_transformation_report_fails_before_continuations_and_tools(position, report):
    owner = Mock()
    original = adapted(owner)
    source = thinking_stream()
    if position == "start":
        source[0]["message"]["input_transformations"] = report
    else:
        source[-2]["input_transformations"] = report
    events = list(adapt_stream(wire(source, "messages", chunk_size=11), original))
    assert_lifecycle(events)
    assert events[-1]["type"] == "response.failed"
    assert not any(event["type"] == "response.output_item.done" for event in events)
    assert not any(event.get("item", {}).get("type") == "reasoning" for event in events)
    assert "synthetic-private" not in str(events)
    owner.seal.assert_not_called()
    assert not get_codex_home().exists()


@pytest.mark.parametrize("delivery", ["buffered", "streamed"])
def test_reported_history_loss_withholds_a_complete_coding_tool_call(delivery):
    owner = Mock()
    original = adapted(owner, tools=[FUNCTION])
    tool = {
        "type": "tool_use",
        "id": "synthetic-call-id",
        "name": original.tools.definitions[0]["name"],
        "input": {"path": "synthetic.txt"},
    }
    report = _TRANSFORMATIONS[0]
    if delivery == "buffered":
        body = messages_reply([SIGNED, tool], finish="tool_use", input_transformations=report)
        with pytest.raises(ProtocolError):
            adapt_response(body, original)
    else:
        source = thinking_stream()[:7] + [
            block(2, tool),
            stop(2),
            {**finish("tool_use"), "input_transformations": report},
            {"type": "message_stop"},
        ]
        events = list(adapt_stream(wire(source, "messages", chunk_size=11), original))
        assert_lifecycle(events)
        assert events[-1]["type"] == "response.failed"
        assert not any(event["type"] == "response.output_item.done" for event in events)
    owner.seal.assert_not_called()
    assert not get_codex_home().exists()


@pytest.mark.parametrize("position", ["start", "fallback"])
@pytest.mark.parametrize("reported_loss", [False, True])
def test_async_runtime_path_handles_empty_and_loss_reports(position, reported_loss):
    source = thinking_stream()
    report = _TRANSFORMATIONS[0] if reported_loss else []
    if position == "start":
        source[0]["message"]["input_transformations"] = report
    else:
        source[-2]["input_transformations"] = report
    original = adapted()

    async def chunks():
        for chunk in wire(source, "messages", chunk_size=11):
            yield chunk

    async def collect():
        return [event async for event in async_adapt_stream(chunks(), original)]

    events = asyncio.run(collect())
    assert_lifecycle(events)
    assert events[-1]["type"] == ("response.failed" if reported_loss else "response.completed")
    if reported_loss:
        assert not any(event["type"] == "response.output_item.done" for event in events)
        assert "synthetic-private" not in str(events)


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
