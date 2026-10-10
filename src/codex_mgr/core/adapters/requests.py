"""Bounded, loss-aware Responses request mappings for Chat and Messages."""

from __future__ import annotations

import base64
import binascii
from copy import deepcopy
from dataclasses import dataclass, field
from urllib.parse import urlparse

from codex_mgr.core.adapters.continuations import ContinuationCodec
from codex_mgr.core.adapters.tools import (
    ProtocolError,
    ToolRegistry,
    encoded_json,
    object_json,
    text,
)

_MAX_REQUEST_BYTES = 8 * 1024 * 1024
_OPTIONS = {
    "model",
    "input",
    "instructions",
    "tools",
    "tool_choice",
    "parallel_tool_calls",
    "reasoning",
    "stream",
    "store",
    "include",
    "max_output_tokens",
    "temperature",
    "top_p",
    "text",
    "previous_response_id",
    "background",
    "metadata",
    "service_tier",
    "prompt_cache_key",
    "client_metadata",
    "stream_options",
    "access_programs",
}


@dataclass(frozen=True)
class AdaptedRequest:
    protocol: str
    model: str
    payload: dict = field(repr=False)
    tools: ToolRegistry = field(repr=False)
    continuations: ContinuationCodec | None = field(default=None, repr=False)


def _image(block, protocol):
    url = text(block.get("image_url"), "Images need an explicit URL.", nonempty=True)
    detail = block.get("detail")
    if detail not in {None, "auto", "low", "high", "original"}:
        raise ProtocolError("Invalid image detail.")
    if url.startswith("data:"):
        metadata, separator, data = url.partition(",")
        media = metadata.removeprefix("data:").removesuffix(";base64")
        if (
            not separator
            or not metadata.endswith(";base64")
            or media
            not in {
                "image/jpeg",
                "image/png",
                "image/gif",
                "image/webp",
            }
        ):
            raise ProtocolError("Unsupported inline image encoding.")
        try:
            if not base64.b64decode(data, validate=True):
                raise ProtocolError("Empty inline image.")
        except (ValueError, binascii.Error) as exc:
            raise ProtocolError("Invalid inline image encoding.") from exc
        source = {"type": "base64", "media_type": media, "data": data}
    else:
        parsed = urlparse(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ProtocolError("Images need an HTTP URL or supported inline encoding.")
        source = {"type": "url", "url": url}
    if protocol == "messages":
        return {"type": "image", "source": source}
    image = {"url": url}
    if detail is not None and detail != "original":
        image["detail"] = detail
    return {"type": "image_url", "image_url": image}


def _content(value, protocol, *, images=True):
    if isinstance(value, str):
        return [{"type": "text", "text": text(value)}]
    if not isinstance(value, list):
        raise ProtocolError("Invalid message content.")
    result = []
    for block in value:
        if not isinstance(block, dict):
            raise ProtocolError("Invalid message content block.")
        kind = block.get("type")
        if kind in {"input_text", "output_text"}:
            result.append({"type": "text", "text": text(block.get("text"))})
        elif kind == "input_image" and images:
            result.append(_image(block, protocol))
        else:
            raise ProtocolError("This content block has no verified adapter mapping.")
    return result


def _append(messages, role, content, *, protocol):
    if protocol == "messages" and messages and messages[-1]["role"] == role:
        messages[-1]["content"].extend(content)
    else:
        messages.append({"role": role, "content": content})


def _history(items, protocol, registry, continuations, model):
    messages, system, pending, seen_calls = [], [], {}, set()
    for item in items:
        if not isinstance(item, dict):
            raise ProtocolError("Invalid Responses history item.")
        kind = item.get("type", "message")
        if kind == "reasoning":
            if pending:
                raise ProtocolError("Tool calls need matching results before reasoning history.")
            if item.get("encrypted_content") is not None:
                if protocol != "messages" or continuations is None:
                    raise ProtocolError("Reasoning history needs a compatible bound continuation.")
                block = continuations.open(item["encrypted_content"], model)
                _append(messages, "assistant", [block], protocol=protocol)
            else:
                summary = item.get("summary")
                if (
                    not isinstance(summary, list)
                    or item.get("content")
                    or item.get("status") not in {None, "completed"}
                ):
                    raise ProtocolError("This readable reasoning history has no verified mapping.")
                content = []
                for part in summary:
                    if (
                        not isinstance(part, dict)
                        or set(part) != {"type", "text"}
                        or part["type"] != "summary_text"
                    ):
                        raise ProtocolError("Invalid readable reasoning summary.")
                    content.append({"type": "text", "text": text(part["text"])})
                if content:
                    _append(messages, "assistant", content, protocol=protocol)
        elif kind == "message":
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise ProtocolError("Invalid Responses message role.")
            if pending:
                raise ProtocolError("Tool calls need matching results before the next message.")
            content = _content(item.get("content"), protocol, images=role == "user")
            if protocol == "messages" and role in {"system", "developer"}:
                system.extend(content)
            else:
                _append(messages, role, content, protocol=protocol)
        elif kind in {"function_call", "custom_tool_call"}:
            if item.get("status") not in {None, "completed"}:
                raise ProtocolError("Tool history contains an unfinished call.")
            call_id = text(item.get("call_id"), "Invalid tool call ID.", nonempty=True)
            if call_id in seen_calls:
                raise ProtocolError("Duplicate tool call ID.")
            seen_calls.add(call_id)
            custom = kind == "custom_tool_call"
            binding = registry.binding(
                item.get("name"), item.get("namespace"), custom=custom, historical=True
            )
            if custom:
                arguments = encoded_json({"input": text(item.get("input"))})
            else:
                arguments = text(item.get("arguments"))
            values = object_json(arguments)
            pending[call_id] = binding
            if protocol == "messages":
                _append(
                    messages,
                    "assistant",
                    [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": binding.wire_name,
                            "input": values,
                        }
                    ],
                    protocol=protocol,
                )
            else:
                if not messages or messages[-1]["role"] != "assistant":
                    messages.append({"role": "assistant", "content": None})
                messages[-1].setdefault("tool_calls", []).append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {"name": binding.wire_name, "arguments": arguments},
                    }
                )
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            call_id = text(item.get("call_id"), "Invalid tool result ID.", nonempty=True)
            binding = pending.get(call_id)
            if binding is None or binding.custom != (kind == "custom_tool_call_output"):
                raise ProtocolError("Tool result has no matching call of the same type.")
            namespace = item.get("namespace")
            if namespace is not None and (namespace or "functions") != (
                binding.namespace or "functions"
            ):
                raise ProtocolError("Tool result conflicts with its call namespace.")
            if item.get("name") is not None and item["name"] != binding.name:
                raise ProtocolError("Tool result conflicts with its call name.")
            del pending[call_id]
            content = _content(item.get("output"), protocol, images=protocol == "messages")
            if protocol == "messages":
                result = {"type": "tool_result", "tool_use_id": call_id, "content": content}
                _append(messages, "user", [result], protocol=protocol)
            else:
                messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
        else:
            raise ProtocolError("This history item has no verified adapter mapping.")
    if pending:
        raise ProtocolError("Tool history ends before its matching results.")
    return messages, system


def _choice(request, protocol, registry):
    choice = request.get("tool_choice", "auto")
    parallel = request.get("parallel_tool_calls", True)
    if not isinstance(parallel, bool):
        raise ProtocolError("Invalid parallel tool control.")
    if isinstance(choice, str) and choice in {"auto", "none", "required"}:
        result = (
            choice if protocol == "chat" else {"type": "any" if choice == "required" else choice}
        )
    elif isinstance(choice, dict) and choice.get("type") in {"function", "custom"}:
        binding = registry.binding(
            choice.get("name"), choice.get("namespace"), custom=choice["type"] == "custom"
        )
        result = (
            {"type": "function", "function": {"name": binding.wire_name}}
            if protocol == "chat"
            else {
                "type": "tool",
                "name": binding.wire_name,
            }
        )
    else:
        raise ProtocolError("This tool choice has no verified adapter mapping.")
    if protocol == "messages" and result["type"] != "none" and not parallel:
        result["disable_parallel_tool_use"] = True
    return result, parallel


def _controls(request, payload, protocol, max_output_tokens, thinking_budget):
    cap = request.get("max_output_tokens")
    if cap is None:
        cap = max_output_tokens
    if cap is None and protocol == "messages":
        cap = 8192  # Adapter policy; not a claim about a model's maximum capacity.
    if cap is not None:
        if not isinstance(cap, int) or isinstance(cap, bool) or cap <= 0:
            raise ProtocolError("Output cap must be a positive integer.")
        payload["max_tokens" if protocol == "messages" else "max_completion_tokens"] = cap
    if thinking_budget is not None:
        if (
            protocol != "messages"
            or not isinstance(thinking_budget, int)
            or isinstance(thinking_budget, bool)
            or thinking_budget < 1024
            or thinking_budget >= cap
        ):
            raise ProtocolError(
                "Messages thinking budget must be at least 1024 and below the output cap."
            )
        payload["thinking"] = {"type": "enabled", "budget_tokens": thinking_budget}
    reasoning = request.get("reasoning")
    if reasoning is not None:
        if not isinstance(reasoning, dict) or set(reasoning) - {"effort", "summary", "context"}:
            raise ProtocolError("Invalid reasoning controls.")
        effort = reasoning.get("effort")
        if reasoning.get("summary") not in {None, "none"} or reasoning.get("context") is not None:
            raise ProtocolError("Reasoning summaries/context need a verified adapter mapping.")
        if effort is not None:
            effort = text(effort, "Invalid reasoning effort.", nonempty=True)
            if protocol == "chat":
                payload["reasoning_effort"] = effort
            elif effort == "none" and thinking_budget is None:
                payload["thinking"] = {"type": "disabled"}
            elif thinking_budget is None or effort == "none":
                raise ProtocolError(
                    "Messages reasoning needs a compatible explicit thinking budget."
                )
    for key in ("temperature", "top_p"):
        if request.get(key) is not None:
            value = request[key]
            upper = 1 if key == "top_p" or protocol == "messages" else 2
            if (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not 0 <= value <= upper
            ):
                raise ProtocolError("Invalid sampling control.")
            if thinking_budget is not None and key == "temperature" and value != 1:
                raise ProtocolError(
                    "Explicit thinking requires temperature 1 or the provider default."
                )
            payload[key] = value
    controls = request.get("text")
    if controls is not None:
        if not isinstance(controls, dict) or set(controls) - {"format", "verbosity"}:
            raise ProtocolError("Invalid text controls.")
        if controls.get("verbosity") is not None:
            if protocol != "chat" or controls["verbosity"] not in {"low", "medium", "high"}:
                raise ProtocolError("This verbosity control has no verified adapter mapping.")
            payload["verbosity"] = controls["verbosity"]
        format_ = controls.get("format")
        if format_ is not None and format_ != {"type": "text"}:
            raise ProtocolError("Structured output needs a verified adapter mapping.")


def _adapt_request(
    request, protocol, *, max_output_tokens=None, thinking_budget=None, continuations=None
):
    """Map a complete supported history; no HTTP, files, logging or input mutation."""
    if protocol not in {"chat", "messages"} or not isinstance(request, dict):
        raise ProtocolError("Choose a supported adapter protocol and JSON request.")
    if len(encoded_json(request).encode()) > _MAX_REQUEST_BYTES:
        raise ProtocolError("Adapter request exceeds the size limit.")
    if set(request) - _OPTIONS:
        raise ProtocolError("This Responses request option has no verified adapter mapping.")
    if request.get("previous_response_id") or request.get("background") or request.get("store"):
        raise ProtocolError(
            "The adapter requires complete input and does not store server responses."
        )
    if request.get("access_programs") or request.get("service_tier") not in {None, "auto"}:
        raise ProtocolError("This service selection has no verified adapter mapping.")
    includes = request.get("include")
    # Codex requests this delivery preference even with reasoning disabled.
    # It does not require a reasoning item when the upstream returns none;
    # opaque history and unsupported upstream reasoning still reject explicitly.
    if includes is not None and (
        not isinstance(includes, list)
        or any(value != "reasoning.encrypted_content" for value in includes)
    ):
        raise ProtocolError("This response delivery option needs a verified adapter mapping.")
    if request.get("stream_options"):
        raise ProtocolError("This response delivery option needs a verified adapter mapping.")
    model = text(request.get("model"), "Supply an exact model ID.", nonempty=True)
    stream = request.get("stream", False)
    if not isinstance(stream, bool):
        raise ProtocolError("Invalid stream control.")
    registry = ToolRegistry.from_tools(request.get("tools"))
    items = request.get("input")
    if isinstance(items, str):
        items = [{"role": "user", "content": items}]
    if not isinstance(items, list) or not items:
        raise ProtocolError(
            "Supply Responses input; the adapter does not invent an initial message."
        )
    messages, system = _history(items, protocol, registry, continuations, model)
    instructions = request.get("instructions")
    if instructions is not None:
        block = {"type": "text", "text": text(instructions)}
        if protocol == "messages":
            system.insert(0, block)
        else:
            messages.insert(0, {"role": "system", "content": [block]})
    if not any(message["role"] in {"user", "assistant", "tool"} for message in messages):
        raise ProtocolError("Supply conversation input in addition to system instructions.")
    payload = {"model": model, "messages": messages, "stream": stream}
    if protocol == "chat" and stream:
        payload["stream_options"] = {"include_usage": True}
    if system:
        payload["system"] = system
    if registry.definitions:
        if protocol == "chat":
            payload["tools"] = [
                {"type": "function", "function": definition} for definition in registry.definitions
            ]
        else:
            payload["tools"] = [
                {
                    "name": definition["name"],
                    "description": definition["description"],
                    "input_schema": definition["parameters"],
                    **({"strict": definition["strict"]} if "strict" in definition else {}),
                }
                for definition in registry.definitions
            ]
        payload["tool_choice"], parallel = _choice(request, protocol, registry)
        if protocol == "chat":
            payload["parallel_tool_calls"] = parallel
    elif request.get("tool_choice", "auto") not in {"auto", "none"}:
        raise ProtocolError("Tool choice requires declared tools.")
    _controls(request, payload, protocol, max_output_tokens, thinking_budget)
    if len(encoded_json(payload).encode()) > _MAX_REQUEST_BYTES:
        raise ProtocolError("Translated request exceeds the size limit.")
    return AdaptedRequest(protocol, model, deepcopy(payload), registry, continuations)


def adapt_request(
    request, protocol, *, max_output_tokens=None, thinking_budget=None, continuations=None
):
    try:
        return _adapt_request(
            request,
            protocol,
            max_output_tokens=max_output_tokens,
            thinking_budget=thinking_budget,
            continuations=continuations,
        )
    except ProtocolError:
        raise
    except (ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ProtocolError("Malformed adapter request.") from exc
