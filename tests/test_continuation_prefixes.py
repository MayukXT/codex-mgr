"""Authenticated continuation prefixes reject rewritten history before HTTP."""

import base64
import json
from copy import deepcopy

import httpx
import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from test_adapter_runtime import headers, payload, synthetic_loopback_http, upstream  # noqa: F401 -- fixture
from test_continuations import REDACTED, SIGNED, codec
from test_protocol_codecs import messages_reply, request
from test_streaming_codecs import assert_lifecycle

from codex_mgr.core.adapters.continuations import MessagesPrefix
from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.responses import adapt_response
from codex_mgr.core.adapters.runtime import AdapterRuntime
from codex_mgr.core.adapters.sse import json_events
from codex_mgr.core.adapters.streaming import buffered_stream
from codex_mgr.core.adapters.tools import ProtocolError

TOOLS = [
    {
        "type": "function",
        "name": name,
        "description": "Synthetic",
        "parameters": {"type": "object", "properties": {}},
    }
    for name in ("first", "second")
]
OTHER = {
    "type": "thinking",
    "thinking": "Other supplied text.",
    "signature": "other-synthetic-signature",
}


def source(**changes):
    return request(
        instructions="  Exact instruction 🌙\n",
        tools=deepcopy(TOOLS),
        reasoning={"effort": "medium", "summary": "auto"},
        input=[
            {"role": "developer", "content": "Exact policy."},
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "  Exact user text.\n"},
                    {"type": "input_image", "image_url": "data:image/png;base64,c3ludGhldGlj"},
                ],
            },
        ],
        **changes,
    )


def translate(value, owner=None, **controls):
    return adapt_request(
        value,
        "messages",
        thinking_budget=controls.pop("thinking_budget", 1024),
        continuations=owner or codec(),
        **controls,
    )


def replay_source(output, initial=None):
    value = deepcopy(initial or source())
    value["input"].extend(deepcopy(output))
    value["input"].append({"role": "user", "content": "Continue."})
    return value


@pytest.mark.parametrize("block", [SIGNED, REDACTED])
def test_v2_hides_commitment_and_requires_actual_prefix_even_after_authenticated_inspection(block):
    owner = codec()
    prefix = MessagesPrefix(
        {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Private synthetic prefix."}]}
            ]
        }
    ).digest
    first, second = (
        owner.seal(block, "model", prefix=prefix),
        owner.seal(block, "model", prefix=prefix),
    )
    assert first.startswith("cmgr2.") and first != second
    assert owner.inspect(first, "model") == block
    assert owner.open(first, "model", prefix=prefix) == block
    assert prefix.hex() not in first and "Private synthetic prefix" not in first
    for actual in (None, b"x" * 32, b"", prefix.hex(), True):
        with pytest.raises(ProtocolError, match="saved conversation prefix") as error:
            owner.open(first, "model", prefix=actual)
        assert "Private synthetic" not in str(error.value)


@pytest.mark.parametrize("changed", ["domain", "binding", "cipher", "key", "model", "connection"])
def test_v2_authentication_cannot_be_bypassed_by_prefix_or_version_changes(changed):
    owner = codec()
    prefix = b"p" * 32
    token = owner.seal(SIGNED, "model", prefix=prefix)
    model = "model"
    if changed == "domain":
        token = token.replace("cmgr2.", "cmgr1.", 1)
    elif changed == "binding":
        token = token.replace(token.split(".")[1], "0" * 64)
    elif changed == "cipher":
        token = token[:-1] + ("A" if token[-1] != "A" else "B")
    elif changed == "key":
        owner = codec(key=b"z" * 32)
    elif changed == "model":
        model = "other-model"
    else:
        owner = codec(credential="other-credential")
    with pytest.raises(ProtocolError):
        owner.open(token, model, prefix=prefix)


def test_legacy_v1_keeps_its_documented_route_model_binding():
    owner = codec()
    token = owner.seal(SIGNED, "model")
    assert token.startswith("cmgr1.") and owner.inspect(token, "model") == SIGNED
    assert owner.open(token, "model") == SIGNED
    with pytest.raises(ProtocolError):
        codec(credential="other").inspect(token, "model")


@pytest.mark.parametrize(
    "document",
    [
        {"block": SIGNED},
        {"block": SIGNED, "prefix": False},
        {"block": SIGNED, "prefix": "a" * 63},
        {"block": SIGNED, "prefix": "A" * 64},
        {"block": SIGNED, "prefix": "a" * 64, "extra": True},
        {"block": {"type": "thinking"}, "prefix": "a" * 64},
        [],
    ],
)
def test_authenticated_but_malformed_v2_contents_are_not_valid_continuations(document):
    owner = codec()
    binding = bytes.fromhex(owner.seal(SIGNED, "model", prefix=b"p" * 32).split(".")[1])
    nonce = b"n" * 12
    cipher = AESGCM(b"k" * 32).encrypt(
        nonce, json.dumps(document).encode(), b"cmgr/messages-thinking/v2\0" + binding
    )
    token = (
        "cmgr2."
        + binding.hex()
        + "."
        + base64.urlsafe_b64encode(nonce + cipher).decode().rstrip("=")
    )
    with pytest.raises(ProtocolError, match="invalid or belongs") as error:
        owner.open(token, "model", prefix=b"p" * 32)
    assert "synthetic" not in str(error.value)


@pytest.mark.parametrize("prefix", [False, "private-synthetic-prefix", b"short", b"x" * 33])
def test_invalid_prefix_cannot_create_key_storage(prefix):
    from unittest.mock import Mock

    from codex_mgr.core.adapters.continuations import ContinuationCodec

    loader = Mock(side_effect=AssertionError("Invalid prefix cannot load/create a key."))
    owner = ContinuationCodec(b"c" * 32, loader)
    with pytest.raises(ProtocolError):
        owner.seal(SIGNED, "model", prefix=prefix)
    loader.assert_not_called()


def test_incremental_commitment_matches_regrouped_messages_and_json_key_order():
    original = translate(source()).payload
    state = MessagesPrefix({"system": original["system"], "tools": original["tools"]})
    for message in original["messages"]:
        for block in message["content"]:
            state.append(message["role"], [block])
    assert state.digest == MessagesPrefix(original).digest
    reordered = deepcopy(original)
    reordered["messages"][0]["content"] = [
        dict(reversed(list(block.items()))) for block in reordered["messages"][0]["content"]
    ]
    assert state.digest == MessagesPrefix(reordered).digest
    assert "Exact" not in repr(state)


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        {"system": None},
        {"tools": {}},
        {"messages": {}},
        {"messages": [False]},
        {"messages": [{"role": [], "content": []}]},
        {"messages": [{"role": "user", "content": ["private"]}]},
    ],
)
def test_malformed_prefix_inputs_have_fixed_errors(invalid):
    with pytest.raises(ProtocolError, match="Invalid conversation prefix"):
        MessagesPrefix(invalid)


@pytest.mark.parametrize("buffered", [False, True])
def test_full_replay_keeps_exact_blocks_and_changed_future_controls(buffered):
    initial = source()
    original = translate(initial)
    body = messages_reply(
        [
            SIGNED,
            {"type": "text", "text": "Between blocks."},
            REDACTED,
            OTHER,
            {"type": "text", "text": "Done."},
        ]
    )
    if buffered:
        events = list(buffered_stream(body, original))
        assert_lifecycle(events)
        output = events[-1]["response"]["output"]
    else:
        output = adapt_response(body, original)["output"]
    value = replay_source(output, initial)
    value["reasoning"] = {"effort": "high", "summary": "none"}
    value["max_output_tokens"] = 8192
    before = deepcopy(value)
    replayed = translate(value, thinking_budget=2048).payload
    assert replayed["messages"][1]["content"] == body["content"]
    assert replayed["thinking"] == {"type": "enabled", "budget_tokens": 2048, "display": "omitted"}
    assert value == before
    assert all(
        item["encrypted_content"].startswith("cmgr2.")
        for item in output
        if item["type"] == "reasoning"
    )


@pytest.mark.parametrize(
    "edit",
    [
        "instructions",
        "early-policy",
        "late-policy",
        "tool-schema",
        "tool-order",
        "user-text",
        "image",
        "drop-first",
        "drop-middle",
        "reorder",
        "duplicate",
        "omit-source",
        "prior-turn",
    ],
)
def test_changed_retained_prefix_fails_before_replay_without_repairing_input(edit):
    initial = source()
    original = translate(initial)
    output = adapt_response(
        messages_reply([SIGNED, REDACTED, OTHER, {"type": "text", "text": "Done."}]), original
    )["output"]
    value = replay_source(output, initial)
    if edit == "instructions":
        value["instructions"] += "changed"
    elif edit == "early-policy":
        value["input"][0]["content"] += "changed"
    elif edit == "late-policy":
        value["input"].append({"role": "developer", "content": "Late changed policy."})
    elif edit == "tool-schema":
        value["tools"][0]["parameters"]["properties"]["extra"] = {"type": "string"}
    elif edit == "tool-order":
        value["tools"].reverse()
    elif edit == "user-text":
        value["input"][1]["content"][0]["text"] += "changed"
    elif edit == "image":
        value["input"][1]["content"][1]["image_url"] = "data:image/png;base64,b3RoZXI="
    elif edit == "drop-first":
        del value["input"][2]
    elif edit == "drop-middle":
        del value["input"][3]
    elif edit == "reorder":
        value["input"][2], value["input"][3] = value["input"][3], value["input"][2]
    elif edit == "duplicate":
        value["input"].insert(3, deepcopy(value["input"][2]))
    elif edit == "omit-source":
        value["input"] = value["input"][2:]
    else:
        value["input"].insert(2, {"role": "user", "content": "Inserted old turn."})
    before = deepcopy(value)
    with pytest.raises(ProtocolError, match="saved conversation prefix") as error:
        translate(value)
    assert "Exact" not in str(error.value) and value == before


def test_prefix_covers_text_and_tool_blocks_between_retained_thinking():
    initial = source()
    original = translate(initial)
    body = messages_reply(
        [
            SIGNED,
            {"type": "tool_use", "id": "call-first", "name": "first", "input": {}},
            {"type": "text", "text": "Between tools."},
            OTHER,
            {"type": "tool_use", "id": "call-second", "name": "second", "input": {}},
        ],
        finish="tool_use",
    )
    output = adapt_response(body, original)["output"]
    value = deepcopy(initial)
    value["input"].extend(output)
    value["input"].extend(
        [
            {"type": "function_call_output", "call_id": name, "output": "Exact result."}
            for name in ("call-first", "call-second")
        ]
    )
    assert translate(value).payload["messages"][1]["content"] == body["content"]
    value["input"][4]["content"][0]["text"] += "changed"
    with pytest.raises(ProtocolError, match="saved conversation prefix"):
        translate(value)


@pytest.mark.parametrize("changed", [False, True])
def test_later_turn_commitment_includes_prior_exact_tool_results(changed):
    value = source()
    first = adapt_response(
        messages_reply(
            [SIGNED, {"type": "tool_use", "id": "call-first", "name": "first", "input": {}}],
            finish="tool_use",
        ),
        translate(value),
    )
    value["input"].extend(first["output"])
    result = {"type": "function_call_output", "call_id": "call-first", "output": "Exact result."}
    value["input"].append(result)
    second = adapt_response(
        messages_reply([OTHER, {"type": "text", "text": "Done."}]), translate(value)
    )
    value["input"].extend(second["output"])
    value["input"].append({"role": "user", "content": "Continue."})
    if changed:
        result["output"] += "changed"
        with pytest.raises(ProtocolError, match="saved conversation prefix"):
            translate(value)
    else:
        replay = translate(value).payload
        assert replay["messages"][2]["content"][0]["content"] == [
            {"type": "text", "text": "Exact result."}
        ]
        assert replay["messages"][3]["content"][0] == OTHER


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("stream", [False, True])
def test_actual_runtime_checks_prefix_before_second_upstream_request(changed, stream, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    raw = json.dumps(messages_reply([SIGNED, {"type": "text", "text": "Done."}])).encode()
    with upstream("messages", buffered=True, raw=raw) as (provider, received, _):
        provider.streaming, provider.reasoning_budget = False, 1024
        with AdapterRuntime(provider) as runtime:
            initial = payload(stream=False)
            initial["reasoning"] = {"effort": "medium", "summary": "none"}
            first = httpx.post(
                runtime.base_url + "/responses",
                headers=headers(runtime),
                json=initial,
                trust_env=False,
            )
            assert first.status_code == 200
            output = first.json()["output"]
            assert runtime.reasoning_compatible(output[0])  # Authenticated inspection only.
            replay = deepcopy(initial)
            replay["stream"] = stream
            replay["input"] = [
                {"role": "user", "content": initial["input"] + ("changed" if changed else "")},
                *output,
                {"role": "user", "content": "Continue."},
            ]
            second = httpx.post(
                runtime.base_url + "/responses",
                headers=headers(runtime),
                json=replay,
                trust_env=False,
            )
            if changed:
                assert second.status_code == 400 and len(received) == 1
                assert "synthetic" not in second.text and SIGNED["signature"] not in second.text
            else:
                assert second.status_code == 200 and len(received) == 2
                if stream:
                    events = list(json_events([second.content]))
                    assert_lifecycle([event.data for event in events])
                    assert events[-1].data["type"] == "response.completed"
