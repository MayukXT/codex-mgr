"""Protocol contracts with synthetic prompts, images and coding tool turns."""

from copy import deepcopy

import pytest

from codex_mgr.core.adapters import (
    ProtocolError,
    adapt_request,
    adapt_response,
    requests,
    responses,
)

FUNCTION = {
    "type": "function",
    "name": "read_file",
    "description": "Read a file.",
    "strict": False,
    "parameters": {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
        "additionalProperties": False,
    },
}
CUSTOM = {
    "type": "custom",
    "name": "apply_patch",
    "description": "Apply a patch.",
    "format": {"type": "grammar", "syntax": "lark", "definition": "start: /.+/"},
}
PATCH = "*** Begin Patch\n*** Add File: synthetic.txt\n+Exact text\n*** End Patch\n"


def request(**values):
    return {"model": "exact/router:model", "input": "Read and update synthetic.txt.", **values}


def chat_reply(content="Done.", *, calls=None, finish=None, **values):
    return {
        "model": "exact/router:model",
        "choices": [
            {
                "message": {"role": "assistant", "content": content, "tool_calls": calls or []},
                "finish_reason": finish or ("tool_calls" if calls else "stop"),
            }
        ],
        **values,
    }


def messages_reply(content=None, *, finish="end_turn", **values):
    return {
        "type": "message",
        "role": "assistant",
        "model": "exact/router:model",
        "content": content if content is not None else [{"type": "text", "text": "Done."}],
        "stop_reason": finish,
        **values,
    }


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_exact_instructions_text_and_images_survive_without_mutating_input(protocol):
    original = request(
        instructions="  Exact instructions.\nKeep whitespace.  ",
        input=[
            {"role": "developer", "content": "Exact developer text.\n"},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "  Exact user text.\n"},
                    {"type": "input_image", "image_url": "data:image/png;base64,c3ludGhldGlj"},
                    {"type": "input_image", "image_url": "https://images.example/synthetic.png"},
                ],
            },
        ],
    )
    before = deepcopy(original)
    adapted = adapt_request(original, protocol)
    assert original == before
    if protocol == "chat":
        assert adapted.payload["messages"][0]["content"][0]["text"] == original["instructions"]
        assert adapted.payload["messages"][1]["content"][0]["text"] == "Exact developer text.\n"
    else:
        assert [block["text"] for block in adapted.payload["system"]] == [
            original["instructions"],
            "Exact developer text.\n",
        ]
    content = adapted.payload["messages"][-1]["content"]
    assert content[0]["text"] == "  Exact user text.\n"
    if protocol == "chat":
        assert content[1]["image_url"]["url"] == "data:image/png;base64,c3ludGhldGlj"
        assert content[2]["image_url"]["url"] == "https://images.example/synthetic.png"
    else:
        assert content[1]["source"] == {
            "type": "base64",
            "media_type": "image/png",
            "data": "c3ludGhldGlj",
        }
        assert content[2]["source"] == {
            "type": "url",
            "url": "https://images.example/synthetic.png",
        }
    assert "Exact user" not in repr(adapted)


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_function_and_freeform_tool_turns_round_trip_namespaces_ids_and_exact_patch(protocol):
    tools = [
        {
            "type": "namespace",
            "name": "editor",
            "description": "Editing tools",
            "tools": [FUNCTION, CUSTOM],
        }
    ]
    original = request(tools=tools, parallel_tool_calls=False)
    adapted = adapt_request(original, protocol)
    read = adapted.tools.bindings[("editor", "read_file")].wire_name
    patch = adapted.tools.bindings[("editor", "apply_patch")].wire_name
    assert read != patch and len(read) <= 64
    if protocol == "chat":
        assert not adapted.payload["parallel_tool_calls"]
        body = chat_reply(
            None,
            calls=[
                {
                    "id": "call-read",
                    "type": "function",
                    "function": {"name": read, "arguments": '{"path":"synthetic.txt"}'},
                },
                {
                    "id": "call-patch",
                    "type": "function",
                    "function": {
                        "name": patch,
                        "arguments": requests.encoded_json({"input": PATCH}),
                    },
                },
            ],
        )
    else:
        assert adapted.payload["tool_choice"]["disable_parallel_tool_use"]
        body = messages_reply(
            [
                {
                    "type": "tool_use",
                    "id": "call-read",
                    "name": read,
                    "input": {"path": "synthetic.txt"},
                },
                {"type": "tool_use", "id": "call-patch", "name": patch, "input": {"input": PATCH}},
            ],
            finish="tool_use",
        )
    output = adapt_response(body, adapted)["output"]
    assert [(item["name"], item["namespace"], item["call_id"]) for item in output] == [
        ("read_file", "editor", "call-read"),
        ("apply_patch", "editor", "call-patch"),
    ]
    assert output[1]["type"] == "custom_tool_call" and output[1]["input"] == PATCH
    continued = request(
        tools=tools,
        input=[
            {"role": "user", "content": original["input"]},
            *output,
            {
                "type": "function_call_output",
                "call_id": "call-read",
                "output": "Original\nfile text.",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call-patch",
                "output": "Patch applied.\n",
            },
            {"role": "user", "content": "Verify the change."},
        ],
    )
    next_request = adapt_request(continued, protocol)
    assert "Original\\nfile text." in requests.encoded_json(next_request.payload)
    assert "Patch applied.\\n" in requests.encoded_json(next_request.payload)
    final = adapt_response(chat_reply() if protocol == "chat" else messages_reply(), next_request)
    assert final["status"] == "completed"
    assert final["output"][0]["content"][0]["text"] == "Done."
    assert final["usage"] is None


@pytest.mark.parametrize("reverse", [False, True])
def test_default_namespace_duplicate_definitions_are_ambiguous(reverse):
    tools = [FUNCTION, {"type": "namespace", "name": "functions", "tools": [FUNCTION]}]
    if reverse:
        tools.reverse()
    with pytest.raises(ProtocolError, match="Duplicate"):
        adapt_request(request(tools=tools), "chat")


def test_separate_namespaces_with_same_tool_name_have_stable_distinct_aliases():
    tools = [
        {"type": "namespace", "name": group, "tools": [FUNCTION]} for group in ["first", "second"]
    ]
    first = adapt_request(request(tools=tools), "chat")
    second = adapt_request(request(tools=list(reversed(tools))), "messages")
    assert first.tools.bindings == second.tools.bindings
    assert len({binding.wire_name for binding in first.tools.bindings.values()}) == 2


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_explicit_tool_choice_uses_reversible_binding(protocol):
    adapted = adapt_request(
        request(tools=[CUSTOM], tool_choice={"type": "custom", "name": "apply_patch"}), protocol
    )
    selected = adapted.payload["tool_choice"]
    assert (
        selected["function"]["name"] if protocol == "chat" else selected["name"]
    ) == "apply_patch"
    with pytest.raises(ProtocolError):
        adapt_request(
            request(tools=[CUSTOM], tool_choice={"type": "function", "name": "apply_patch"}),
            protocol,
        )


def test_tool_choice_cannot_select_an_undeclared_historical_tool():
    original = request(
        tools=[CUSTOM],
        tool_choice={"type": "function", "name": "read_file"},
        input=[
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "historical",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "historical", "output": "Old result."},
        ],
    )
    with pytest.raises(ProtocolError, match="undeclared"):
        adapt_request(original, "chat")


@pytest.mark.parametrize("field,value", [("namespace", "other"), ("name", "other")])
def test_tool_result_cannot_conflict_with_its_call_identity(field, value):
    original = request(
        input=[
            {
                "type": "function_call",
                "namespace": "editor",
                "name": "read_file",
                "call_id": "call",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call", "output": "Result.", field: value},
        ]
    )
    with pytest.raises(ProtocolError, match="conflicts"):
        adapt_request(original, "messages")


def test_reused_call_ids_and_unfinished_calls_cannot_be_accepted_as_complete_history():
    call = {"type": "function_call", "name": "read_file", "call_id": "call", "arguments": "{}"}
    result = {"type": "function_call_output", "call_id": "call", "output": "Result."}
    with pytest.raises(ProtocolError, match="Duplicate"):
        adapt_request(request(input=[call, result, call, result]), "chat")
    with pytest.raises(ProtocolError, match="unfinished"):
        adapt_request(request(input=[dict(call, status="incomplete"), result]), "chat")


def test_stream_requests_explicitly_request_chat_usage():
    assert adapt_request(request(stream=True), "chat").payload["stream_options"] == {
        "include_usage": True
    }


def test_messages_refusal_remains_a_refusal():
    result = adapt_response(messages_reply(finish="refusal"), adapt_request(request(), "messages"))
    assert result["output"][0]["content"] == [{"type": "refusal", "refusal": "Done."}]


def test_messages_tool_result_images_are_preserved_and_chat_rejects_unsupported_role_images():
    original = request(
        tools=[FUNCTION],
        input=[
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "image-call",
                "arguments": '{"path":"image.png"}',
            },
            {
                "type": "function_call_output",
                "call_id": "image-call",
                "output": [
                    {"type": "input_text", "text": "Tool image."},
                    {"type": "input_image", "image_url": "https://images.example/synthetic.png"},
                ],
            },
        ],
    )
    result = adapt_request(original, "messages").payload["messages"][-1]["content"][0]
    assert result["type"] == "tool_result" and result["tool_use_id"] == "image-call"
    assert result["content"][1]["type"] == "image"
    with pytest.raises(ProtocolError):
        adapt_request(original, "chat")


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize(
    "history",
    [
        [{"type": "function_call_output", "call_id": "missing", "output": "synthetic-private"}],
        [{"type": "function_call", "name": "read_file", "call_id": "pending", "arguments": "{}"}],
        [
            {
                "type": "function_call",
                "name": "read_file",
                "call_id": "wrong-kind",
                "arguments": "{}",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "wrong-kind",
                "output": "synthetic-private",
            },
        ],
        [{"type": "reasoning", "encrypted_content": "synthetic-private", "summary": []}],
        [{"role": "user", "content": [{"type": "input_audio", "data": "synthetic-private"}]}],
    ],
)
def test_untranslatable_history_fails_without_partial_content_or_raw_errors(protocol, history):
    with pytest.raises(ProtocolError) as failure:
        adapt_request(request(input=history, tools=[FUNCTION]), protocol)
    assert "synthetic-private" not in str(failure.value)


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("input", []),
        ("input", None),
        ("model", None),
        ("store", True),
        ("previous_response_id", "synthetic-private"),
        ("stream", "true"),
        ("reasoning", {"effort": []}),
        ("tool_choice", {"type": "unknown"}),
        ("temperature", True),
        ("unknown_semantic_option", "synthetic-private"),
    ],
)
def test_malformed_or_unsupported_request_values_return_fixed_protocol_errors(
    protocol, field, value
):
    with pytest.raises(ProtocolError) as failure:
        adapt_request(request(**{field: value}), protocol)
    assert "synthetic-private" not in str(failure.value)


def test_output_caps_and_explicit_thinking_budget_are_not_guessed_from_effort():
    default = adapt_request(request(), "messages")
    assert default.payload["max_tokens"] == 8192 and "thinking" not in default.payload
    enabled = adapt_request(
        request(reasoning={"effort": "high"}),
        "messages",
        max_output_tokens=4096,
        thinking_budget=1024,
    )
    assert enabled.payload["max_tokens"] == 4096
    assert enabled.payload["thinking"] == {"type": "enabled", "budget_tokens": 1024}
    with pytest.raises(ProtocolError):
        adapt_request(request(reasoning={"effort": "high"}), "messages")
    with pytest.raises(ProtocolError):
        adapt_request(request(), "messages", max_output_tokens=1024, thinking_budget=1024)
    chat = adapt_request(request(max_output_tokens=2048, reasoning={"effort": "high"}), "chat")
    assert (
        chat.payload["max_completion_tokens"] == 2048 and chat.payload["reasoning_effort"] == "high"
    )


def test_both_sides_reject_oversized_payloads(monkeypatch):
    adapted = adapt_request(request(), "chat")
    monkeypatch.setattr(requests, "_MAX_REQUEST_BYTES", 128)
    with pytest.raises(ProtocolError, match="size limit"):
        adapt_request(request(input="x" * 200), "chat")
    monkeypatch.setattr(responses, "_MAX_RESPONSE_BYTES", 128)
    with pytest.raises(ProtocolError, match="size limit"):
        adapt_response(chat_reply("x" * 200), adapted)


def test_chat_usage_incomplete_and_refusal_have_truthful_responses_fields():
    adapted = adapt_request(request(), "chat")
    body = chat_reply(
        "Partial",
        finish="length",
        usage={
            "prompt_tokens": 12,
            "completion_tokens": 4,
            "total_tokens": 16,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens_details": {"reasoning_tokens": 2},
        },
    )
    result = adapt_response(body, adapted)
    assert result["object"] == "response" and result["status"] == "incomplete"
    assert result["incomplete_details"] == {"reason": "max_output_tokens"}
    assert result["usage"]["input_tokens_details"]["cached_tokens"] == 5
    assert result["usage"]["output_tokens_details"]["reasoning_tokens"] == 2
    body = chat_reply(None, finish="content_filter")
    body["choices"][0]["message"]["refusal"] = "Cannot comply."
    result = adapt_response(body, adapted)
    assert result["output"][0]["content"] == [{"type": "refusal", "refusal": "Cannot comply."}]
    assert result["incomplete_details"]["reason"] == "content_filter"


def test_messages_cache_creation_and_reads_are_included_in_total_input_usage():
    adapted = adapt_request(request(), "messages")
    result = adapt_response(
        messages_reply(
            usage={
                "input_tokens": 3,
                "cache_creation_input_tokens": 4,
                "cache_read_input_tokens": 5,
                "output_tokens": 6,
            }
        ),
        adapted,
    )
    assert result["usage"]["input_tokens"] == 12 and result["usage"]["total_tokens"] == 18
    assert result["usage"]["input_tokens_details"]["cached_tokens"] == 5


@pytest.mark.parametrize("arguments", ["{", "[]", '{"path":"a","path":"b"}', '{"path":NaN}'])
def test_invalid_function_arguments_never_publish_a_call(arguments):
    adapted = adapt_request(request(tools=[FUNCTION]), "chat")
    body = chat_reply(
        None,
        calls=[
            {
                "id": "call",
                "type": "function",
                "function": {"name": "read_file", "arguments": arguments},
            }
        ],
    )
    with pytest.raises(ProtocolError):
        adapt_response(body, adapted)


@pytest.mark.parametrize("name", ["not_declared", "synthetic-private", None])
def test_undeclared_upstream_calls_are_rejected_without_echoing_names(name):
    adapted = adapt_request(request(tools=[FUNCTION]), "chat")
    body = chat_reply(
        None,
        calls=[{"id": "call", "type": "function", "function": {"name": name, "arguments": "{}"}}],
    )
    with pytest.raises(ProtocolError) as failure:
        adapt_response(body, adapted)
    assert "synthetic-private" not in str(failure.value)


def test_truncated_tool_calls_signed_thinking_and_raw_upstream_errors_are_rejected():
    chat = adapt_request(request(tools=[FUNCTION]), "chat")
    with pytest.raises(ProtocolError):
        adapt_response(
            chat_reply(
                None,
                finish="length",
                calls=[
                    {
                        "id": "call",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    },
                ],
            ),
            chat,
        )
    messages = adapt_request(request(), "messages")
    with pytest.raises(ProtocolError):
        adapt_response(
            messages_reply(
                [
                    {
                        "type": "thinking",
                        "thinking": "synthetic-private",
                        "signature": "synthetic-private",
                    }
                ]
            ),
            messages,
        )
    with pytest.raises(ProtocolError) as failure:
        adapt_response({"error": {"message": "synthetic-private"}}, chat)
    assert "synthetic-private" not in str(failure.value)
