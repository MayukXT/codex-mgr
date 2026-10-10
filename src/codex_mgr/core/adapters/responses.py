"""Buffered upstream replies become validated Responses output items."""

from __future__ import annotations

import time
import uuid

from codex_mgr.core.adapters.requests import AdaptedRequest
from codex_mgr.core.adapters.tools import ProtocolError, encoded_json, text

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024


def identifier(prefix):
    return prefix + uuid.uuid4().hex


def _tokens(value):
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ProtocolError("Invalid upstream token usage.")
    return value


def response_usage(usage, protocol):
    if usage is None:
        return None
    if not isinstance(usage, dict):
        raise ProtocolError("Invalid upstream token usage.")
    if protocol == "messages":
        cached = _tokens(usage.get("cache_read_input_tokens", 0))
        written = _tokens(usage.get("cache_creation_input_tokens", 0))
        inputs = _tokens(usage.get("input_tokens")) + cached + written
        outputs = _tokens(usage.get("output_tokens"))
        reasoning = 0
    else:
        inputs = _tokens(usage.get("prompt_tokens"))
        outputs = _tokens(usage.get("completion_tokens"))
        input_details = usage.get("prompt_tokens_details") or {}
        output_details = usage.get("completion_tokens_details") or {}
        if not isinstance(input_details, dict) or not isinstance(output_details, dict):
            raise ProtocolError("Invalid upstream usage details.")
        cached = _tokens(input_details.get("cached_tokens", 0))
        reasoning = _tokens(output_details.get("reasoning_tokens", 0))
        if (
            cached > inputs
            or reasoning > outputs
            or (
                usage.get("total_tokens") is not None
                and _tokens(usage["total_tokens"]) != inputs + outputs
            )
        ):
            raise ProtocolError("Inconsistent upstream token usage.")
    result = {
        "input_tokens": inputs,
        "output_tokens": outputs,
        "total_tokens": inputs + outputs,
        "input_tokens_details": {"cached_tokens": cached},
        "output_tokens_details": {"reasoning_tokens": reasoning},
    }
    if protocol == "messages" and written:
        result["input_tokens_details"]["cache_write_tokens"] = written
    return result


def message_item(content, *, incomplete=False):
    return {
        "id": identifier("msg_"),
        "type": "message",
        "role": "assistant",
        "status": "incomplete" if incomplete else "completed",
        "content": content,
    }


def tool_item(registry, wire_name, call_id, arguments):
    call = registry.upstream_call(wire_name, call_id, arguments)
    call["id"] = identifier("fc_" if call["type"] == "function_call" else "ctc_")
    if call["type"] == "function_call":
        call["status"] = "completed"
    return call


def _chat_output(body, adapted):
    choices = body.get("choices")
    if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
        raise ProtocolError("The adapter requires one upstream completion choice.")
    choice = choices[0]
    message = choice.get("message")
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise ProtocolError("Invalid upstream assistant message.")
    if any(
        message.get(key)
        for key in ("reasoning_content", "reasoning", "audio", "function_call", "annotations")
    ):
        raise ProtocolError("This upstream content needs a verified response mapping.")
    finish = choice.get("finish_reason")
    if finish not in {"stop", "tool_calls", "length", "content_filter"}:
        raise ProtocolError("Unsupported upstream finish reason.")
    incomplete = finish in {"length", "content_filter"}
    content = []
    if message.get("content") is not None:
        content.append({"type": "output_text", "text": text(message["content"]), "annotations": []})
    if message.get("refusal") is not None:
        content.append({"type": "output_text", "text": text(message["refusal"]), "annotations": []})
    output = [message_item(content, incomplete=incomplete)] if content else []
    calls = message.get("tool_calls") or []
    if (
        not isinstance(calls, list)
        or (calls and finish != "tool_calls")
        or (finish == "tool_calls" and not calls)
    ):
        raise ProtocolError("Upstream tool calls conflict with the finish reason.")
    seen = set()
    for call in calls:
        if (
            not isinstance(call, dict)
            or call.get("type") != "function"
            or not isinstance(call.get("function"), dict)
        ):
            raise ProtocolError("Invalid upstream tool call.")
        item = tool_item(
            adapted.tools,
            call["function"].get("name"),
            call.get("id"),
            call["function"].get("arguments"),
        )
        if item["call_id"] in seen:
            raise ProtocolError("Duplicate upstream tool call ID.")
        seen.add(item["call_id"])
        output.append(item)
    if not output:
        raise ProtocolError("Upstream omitted its response content.")
    return (
        output,
        incomplete,
        "content_filter" if finish == "content_filter" else "max_output_tokens",
    )


def _messages_output(body, adapted):
    if (
        body.get("type") != "message"
        or body.get("role") != "assistant"
        or not isinstance(body.get("content"), list)
    ):
        raise ProtocolError("Invalid upstream Messages response.")
    finish = body.get("stop_reason")
    if finish not in {"end_turn", "stop_sequence", "tool_use", "max_tokens", "refusal"}:
        raise ProtocolError("Unsupported upstream stop reason.")
    incomplete = finish == "max_tokens"
    output, seen = [], set()
    for block in body["content"]:
        if not isinstance(block, dict):
            raise ProtocolError("Invalid upstream Messages content.")
        if block.get("citations"):
            raise ProtocolError("Upstream citations need a verified response mapping.")
        if block.get("type") == "text":
            content = {"type": "output_text", "text": text(block.get("text")), "annotations": []}
            output.append(
                message_item(
                    [content],
                    incomplete=incomplete,
                )
            )
        elif block.get("type") in {"thinking", "redacted_thinking"}:
            if (
                adapted.continuations is None
                or adapted.payload.get("thinking", {}).get("type") != "enabled"
            ):
                raise ProtocolError("Messages thinking needs an enabled, bound continuation.")
            output.append(
                {
                    "type": "reasoning",
                    "id": identifier("rs_"),
                    "summary": [],
                    "encrypted_content": adapted.continuations.seal(block, adapted.model),
                }
            )
        elif block.get("type") == "tool_use" and finish == "tool_use":
            if not isinstance(block.get("input"), dict):
                raise ProtocolError("Invalid upstream tool arguments.")
            item = tool_item(
                adapted.tools, block.get("name"), block.get("id"), encoded_json(block["input"])
            )
            if item["call_id"] in seen:
                raise ProtocolError("Duplicate upstream tool call ID.")
            seen.add(item["call_id"])
            output.append(item)
        else:
            raise ProtocolError("This upstream block needs a verified response mapping.")
    if not output or (finish == "tool_use" and not seen):
        raise ProtocolError("Upstream omitted its response content or tool calls.")
    return output, incomplete, "max_output_tokens"


def adapt_response(body, adapted: AdaptedRequest):
    """Validate every output item before publishing any translated response."""
    try:
        if not isinstance(body, dict) or body.get("error"):
            raise ProtocolError("Upstream returned an invalid or failed response.")
        if len(encoded_json(body).encode()) > _MAX_RESPONSE_BYTES:
            raise ProtocolError("Upstream response exceeds the size limit.")
        output, incomplete, reason = (
            _chat_output(body, adapted)
            if adapted.protocol == "chat"
            else _messages_output(body, adapted)
        )
        model = text(body.get("model", adapted.model), "Invalid upstream model ID.", nonempty=True)
        response = {
            "id": identifier("resp_"),
            "object": "response",
            "created_at": int(time.time()),
            "status": "incomplete" if incomplete else "completed",
            "model": model,
            "output": output,
            "error": None,
            "incomplete_details": {"reason": reason} if incomplete else None,
            "usage": response_usage(body.get("usage"), adapted.protocol),
        }
        # Native Codex reads output_text but has no refusal content variant.
        # Keep the exact wording readable and retain its classification separately.
        refused = (
            body["choices"][0]["message"].get("refusal") is not None
            if adapted.protocol == "chat"
            else body.get("stop_reason") == "refusal"
        )
        if refused:
            response["metadata"] = {"upstream_stop_reason": "refusal"}
        return response
    except ProtocolError:
        raise
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ProtocolError("Malformed upstream response.") from exc
