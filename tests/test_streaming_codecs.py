"""Synthetic incremental coding, refusal, incomplete and corrupted stream contracts."""

import json
from copy import deepcopy

import pytest

from codex_mgr.core.adapters import adapt_request, adapt_stream
from codex_mgr.core.adapters.sse import encode_event

MODEL = "synthetic/exact:model"
TOOLS = [
    {
        "type": "namespace",
        "name": "editor",
        "tools": [
            {"type": "function", "name": "read_file", "parameters": {"type": "object"}},
            {"type": "custom", "name": "apply_patch", "format": {"type": "text"}},
        ],
    }
]
PATCH = "*** Begin Patch\n*** Add File: synthetic.txt\n+Exact 🌙\n*** End Patch\n"


def request(protocol, **options):
    return adapt_request(
        {
            "model": MODEL,
            "input": "Inspect synthetic.txt.",
            "tools": TOOLS,
            "stream": True,
            **options,
        },
        protocol,
    )


def chat(delta=None, finish=None, **options):
    return {
        "id": "synthetic-chat-id",
        "object": "chat.completion.chunk",
        "model": MODEL,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish}],
        **options,
    }


def start():
    return {
        "type": "message_start",
        "message": {
            "id": "synthetic-message-id",
            "type": "message",
            "role": "assistant",
            "model": MODEL,
            "content": [],
            "stop_reason": None,
            "usage": {
                "input_tokens": 3,
                "output_tokens": 1,
                "cache_read_input_tokens": 5,
                "cache_creation_input_tokens": 4,
            },
        },
    }


def block(index, value):
    return {"type": "content_block_start", "index": index, "content_block": value}


def delta(index, value, *, tool=False):
    return {
        "type": "content_block_delta",
        "index": index,
        "delta": {
            "type": "input_json_delta" if tool else "text_delta",
            "partial_json" if tool else "text": value,
        },
    }


def stop(index):
    return {"type": "content_block_stop", "index": index}


def finish(reason="end_turn", usage=None):
    return {
        "type": "message_delta",
        "delta": {"stop_reason": reason, "stop_sequence": None},
        "usage": usage or {"output_tokens": 7},
    }


def wire(events, protocol, *, chunk_size=None):
    source = b""
    for event in events:
        if isinstance(event, bytes):
            source += event
        elif event == "[DONE]":
            source += b"data: [DONE]\r\n\r\n"
        else:
            name = "event: " + event["type"] + "\r\n" if protocol == "messages" else ""
            source += (
                name + "data: " + json.dumps(event, ensure_ascii=False) + "\r\n\r\n"
            ).encode()
    return (
        [source]
        if chunk_size is None
        else [source[i : i + chunk_size] for i in range(0, len(source), chunk_size)]
    )


def text_stream(protocol, *, reason=None):
    if protocol == "chat":
        return [
            chat({"role": "assistant", "content": "  Exact "}),
            chat({"content": "🌙\n"}),
            chat(finish=reason or "stop"),
            "[DONE]",
        ]
    return [
        start(),
        {"type": "ping"},
        block(0, {"type": "text", "text": "  Exact "}),
        delta(0, "🌙\n"),
        stop(0),
        finish(reason or "end_turn"),
        {"type": "message_stop"},
    ]


def assert_lifecycle(events):
    assert [event["sequence_number"] for event in events] == list(range(len(events)))
    assert events[0]["type"] == "response.created"
    terminal = events[-1]
    assert terminal["type"] in {"response.completed", "response.incomplete", "response.failed"}
    assert terminal["response"]["id"] == events[0]["response"]["id"]
    for event in events:
        assert encode_event(event).startswith(b"event: response.")
    if terminal["type"] != "response.failed":
        output = terminal["response"]["output"]
        for event in events:
            if event["type"] == "response.output_item.done":
                assert event["item"] == output[event["output_index"]]
            if "item_id" in event:
                assert event["item_id"] == output[event["output_index"]]["id"]


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize("chunk_size", [None, 1, 3, 127])
def test_exact_incremental_text_and_stable_lifecycle_at_utf8_crlf_boundaries(protocol, chunk_size):
    events = list(
        adapt_stream(
            wire(text_stream(protocol), protocol, chunk_size=chunk_size), request(protocol)
        )
    )
    assert_lifecycle(events)
    assert (
        "".join(event["delta"] for event in events if event["type"] == "response.output_text.delta")
        == "  Exact 🌙\n"
    )
    response = events[-1]["response"]
    assert response["status"] == "completed"
    assert response["output"][0]["content"][0]["text"] == "  Exact 🌙\n"
    if protocol == "messages":
        assert response["usage"]["input_tokens"] == 12
        assert response["usage"]["output_tokens"] == 7
        assert response["usage"]["input_tokens_details"] == {
            "cached_tokens": 5,
            "cache_write_tokens": 4,
        }
    else:
        assert response["usage"] is None


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_text_is_yielded_before_reading_the_next_upstream_record(protocol):
    reads = []

    def chunks():
        for event in text_stream(protocol):
            reads.append(event)
            yield wire([event], protocol)[0]

    stream = adapt_stream(chunks(), request(protocol))
    assert next(stream)["type"] == "response.created"
    assert not reads
    while next(stream)["type"] != "response.output_text.delta":
        pass
    assert len(reads) == (1 if protocol == "chat" else 3)
    stream.close()


def coding_stream(protocol, adapted):
    names = [definition["name"] for definition in adapted.tools.definitions]
    args = ['{"path":"synthetic.txt"}', json.dumps({"input": PATCH}, ensure_ascii=False)]
    if protocol == "chat":
        events = []
        for i, (name, value) in enumerate(zip(names, args)):
            events.append(
                chat(
                    {
                        "tool_calls": [
                            {
                                "index": i,
                                "id": f"call-{i}",
                                "type": "function",
                                "function": {"name": name[:10], "arguments": value[:2]},
                            }
                        ]
                    }
                )
            )
            events.append(
                chat(
                    {
                        "tool_calls": [
                            {"index": i, "function": {"name": name[10:], "arguments": value[2:]}}
                        ]
                    }
                )
            )
        return events + [chat(finish="tool_calls"), "[DONE]"]
    events = [start()]
    for i, (name, value) in enumerate(zip(names, args)):
        events += [
            block(i, {"type": "tool_use", "id": f"call-{i}", "name": name, "input": {}}),
            delta(i, value[:2], tool=True),
            delta(i, value[2:], tool=True),
            stop(i),
        ]
    return events + [finish("tool_use"), {"type": "message_stop"}]


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_function_custom_tools_and_followup_preserve_namespace_ids_and_exact_patch(protocol):
    adapted = request(protocol)
    consumed = []

    def chunks():
        yield from wire(coding_stream(protocol, adapted), protocol, chunk_size=7)
        consumed.append("eof")

    events = []
    for event in adapt_stream(chunks(), adapted):
        if event["type"] == "response.output_item.done":
            assert consumed == ["eof"]
        events.append(event)
    assert_lifecycle(events)
    calls = events[-1]["response"]["output"]
    assert [call["call_id"] for call in calls] == ["call-0", "call-1"]
    assert [call["namespace"] for call in calls] == ["editor", "editor"]
    assert json.loads(calls[0]["arguments"]) == {"path": "synthetic.txt"}
    if protocol == "chat":
        assert calls[0]["arguments"] == '{"path":"synthetic.txt"}'
    assert calls[1]["input"] == PATCH
    custom_delta = next(
        event for event in events if event["type"] == "response.custom_tool_call_input.delta"
    )
    assert custom_delta["delta"] == PATCH
    history = [{"role": "user", "content": "Inspect synthetic.txt."}]
    for call in calls:
        history += [
            call,
            {
                "type": "custom_tool_call_output"
                if call["type"] == "custom_tool_call"
                else "function_call_output",
                "call_id": call["call_id"],
                "output": "Exact synthetic result.\n",
            },
        ]
    followup = request(protocol, input=history)
    assert any(
        "Exact synthetic result." in json.dumps(item) for item in followup.payload["messages"]
    )
    final = list(adapt_stream(wire(text_stream(protocol), protocol), followup))
    assert final[-1]["type"] == "response.completed"


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_refusal_text_is_native_readable_with_classification_preserved(protocol):
    if protocol == "chat":
        source = [
            chat({"refusal": "Cannot "}),
            chat({"refusal": "comply.\n"}),
            chat(finish="stop"),
            "[DONE]",
        ]
    else:
        source = [
            start(),
            block(0, {"type": "text", "text": "Cannot "}),
            delta(0, "comply.\n"),
            stop(0),
            finish("refusal"),
            {"type": "message_stop"},
        ]
    events = list(adapt_stream(wire(source, protocol), request(protocol)))
    assert_lifecycle(events)
    assert events[-1]["response"]["metadata"] == {"upstream_stop_reason": "refusal"}
    assert events[-1]["response"]["output"][0]["content"] == [
        {"type": "output_text", "text": "Cannot comply.\n", "annotations": []}
    ]


@pytest.mark.parametrize(
    "protocol,reason", [("chat", "length"), ("chat", "content_filter"), ("messages", "max_tokens")]
)
def test_incomplete_text_never_becomes_a_successful_terminal(protocol, reason):
    events = list(
        adapt_stream(wire(text_stream(protocol, reason=reason), protocol), request(protocol))
    )
    assert_lifecycle(events)
    assert events[-1]["type"] == "response.incomplete"
    assert events[-1]["response"]["output"][0]["status"] == "incomplete"
    assert events[-1]["response"]["incomplete_details"]["reason"] == (
        "content_filter" if reason == "content_filter" else "max_output_tokens"
    )


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize(
    "corruption",
    [
        "truncate",
        "after_terminal",
        "bad_json",
        "error",
        "duplicate_id",
        "invalid_args",
        "unknown_tool",
        "incomplete_call",
    ],
)
def test_corruption_never_publishes_an_executable_tool_or_raw_diagnostics(protocol, corruption):
    adapted = request(protocol)
    source = coding_stream(protocol, adapted)
    secret = "synthetic-private-marker"
    if corruption == "truncate":
        source = source[:-1]
    elif corruption == "after_terminal":
        source += [chat({"content": secret}) if protocol == "chat" else {"type": "ping"}]
    elif corruption == "bad_json":
        source += [b'data: {"synthetic-private-marker":\n\n']
    elif corruption == "error":
        source = source[:-1] + [{"type": "error", "error": {"message": secret}}]
    elif corruption == "duplicate_id":
        if protocol == "chat":
            source[2]["choices"][0]["delta"]["tool_calls"][0]["id"] = "call-0"
        else:
            source[5]["content_block"]["id"] = "call-0"
    elif corruption == "invalid_args":
        if protocol == "chat":
            source[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] = secret
        else:
            source[2]["delta"]["partial_json"] = secret
    elif corruption == "unknown_tool":
        if protocol == "chat":
            source[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] = secret
        else:
            source[1]["content_block"]["name"] = secret
    else:
        if protocol == "chat":
            source[-2]["choices"][0]["finish_reason"] = "length"
        else:
            source[-2]["delta"]["stop_reason"] = "max_tokens"
    events = list(adapt_stream(wire(source, protocol), adapted))
    assert events[-1]["type"] == "response.failed"
    assert not any(event["type"] == "response.output_item.done" for event in events)
    assert secret not in json.dumps(events)
    assert_lifecycle(events)


@pytest.mark.parametrize(
    "mutation",
    [
        "id",
        "model",
        "choice",
        "role",
        "reasoning",
        "audio",
        "index",
        "call_id",
        "usage",
        "finish",
        "sentinel",
    ],
)
def test_chat_identity_metadata_indexes_and_finish_state_are_strict(mutation):
    adapted = request("chat")
    source = coding_stream("chat", adapted)
    target = source[1]
    if mutation in {"id", "model"}:
        target[mutation] = "different"
    elif mutation == "choice":
        target["choices"].append(deepcopy(target["choices"][0]))
    elif mutation in {"role", "reasoning", "audio"}:
        target["choices"][0]["delta"][mutation] = "unsupported"
    elif mutation == "index":
        source[0]["choices"][0]["delta"]["tool_calls"][0]["index"] = 2
    elif mutation == "call_id":
        target["choices"][0]["delta"]["tool_calls"][0]["id"] = "changed"
    elif mutation == "usage":
        source.insert(
            -1,
            chat(
                choices=[], usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 99}
            ),
        )
    elif mutation == "finish":
        source.insert(-1, chat(finish="tool_calls"))
    else:
        source[-1] = "[DONE] "
    events = list(adapt_stream(wire(source, "chat"), adapted))
    assert events[-1]["type"] == "response.failed"
    assert not any(event["type"] == "response.output_item.done" for event in events)


@pytest.mark.parametrize(
    "mutation",
    [
        "start",
        "role",
        "index",
        "delta_type",
        "block_overlap",
        "stop",
        "finish",
        "thinking",
        "citations",
        "usage",
        "name",
        "sentinel",
    ],
)
def test_messages_content_order_identity_and_cumulative_usage_are_strict(mutation):
    source = text_stream("messages")
    if mutation == "start":
        source = source[1:]
    elif mutation == "role":
        source[0]["message"]["role"] = "user"
    elif mutation == "index":
        source[3]["index"] = True
    elif mutation == "delta_type":
        source[3]["delta"]["type"] = "signature_delta"
    elif mutation == "block_overlap":
        source.insert(4, block(1, {"type": "text", "text": "Overlap"}))
    elif mutation == "stop":
        source.pop(4)
    elif mutation == "finish":
        source[5]["delta"]["stop_reason"] = "pause_turn"
    elif mutation == "thinking":
        source[2]["content_block"] = {
            "type": "thinking",
            "thinking": "Private",
            "signature": "Opaque",
        }
    elif mutation == "citations":
        source[2]["content_block"]["citations"] = [{"type": "page_location"}]
    elif mutation == "usage":
        source[5]["usage"]["output_tokens"] = 0
    elif mutation == "name":
        source.insert(4, b'event: error\ndata: {"type":"ping"}\n\n')
    else:
        source[-1] = "[DONE]"
    events = list(adapt_stream(wire(source, "messages"), request("messages")))
    assert events[-1]["type"] == "response.failed"
    assert not any(event["type"] == "response.output_item.done" for event in events)


def test_chat_final_usage_only_chunk_is_counted_once():
    source = text_stream("chat")
    source.insert(
        -1, chat(choices=[], usage={"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11})
    )
    events = list(adapt_stream(wire(source, "chat"), request("chat")))
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"]["total_tokens"] == 11
    source.insert(-1, deepcopy(source[-2]))
    events = list(adapt_stream(wire(source, "chat"), request("chat")))
    assert events[-1]["type"] == "response.failed"


def test_messages_mixed_tool_and_text_blocks_keep_output_order_and_ids():
    adapted = request("messages")
    source = coding_stream("messages", adapted)
    source[5:5] = [block(1, {"type": "text", "text": "After read.\n"}), stop(1)]
    for event in source[7:-2]:
        event["index"] = 2
    events = list(adapt_stream(wire(source, "messages"), adapted))
    assert_lifecycle(events)
    assert [item["type"] for item in events[-1]["response"]["output"]] == [
        "function_call",
        "message",
        "custom_tool_call",
    ]
    assert events[-1]["response"]["output"][1]["content"][0]["text"] == "After read.\n"


def test_messages_cumulative_usage_updates_merge_without_double_counting():
    source = text_stream("messages")
    source.insert(
        -2, {"type": "message_delta", "delta": {"stop_reason": None}, "usage": {"output_tokens": 4}}
    )
    events = list(adapt_stream(wire(source, "messages"), request("messages")))
    assert events[-1]["type"] == "response.completed"
    assert events[-1]["response"]["usage"]["output_tokens"] == 7


def test_chat_usage_cannot_be_a_premature_final_snapshot():
    source = text_stream("chat")
    source[0]["usage"] = {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11}
    events = list(adapt_stream(wire(source, "chat"), request("chat")))
    assert events[-1]["type"] == "response.failed"


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_upstream_io_error_size_limit_and_generator_cancellation(protocol):
    def broken():
        yield wire(text_stream(protocol)[:1], protocol)[0]
        raise OSError("synthetic-private-error")

    events = list(adapt_stream(broken(), request(protocol)))
    assert events[-1]["type"] == "response.failed"
    assert "synthetic-private-error" not in json.dumps(events)
    events = list(
        adapt_stream(wire(text_stream(protocol), protocol), request(protocol), max_total_bytes=32)
    )
    assert events[-1]["type"] == "response.failed"
    consumed = []

    def cancelled():
        consumed.append(True)
        yield b"data: {}\n\n"

    stream = adapt_stream(cancelled(), request(protocol))
    next(stream)
    stream.close()
    assert consumed == []
