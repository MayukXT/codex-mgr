"""Validated upstream stream assembly and Responses lifecycle events.

Text is incremental. Executable tool items are withheld until the complete
upstream stream, finish state, arguments and usage have all been validated.
The transport owner must close its upstream response when this iterator closes.
"""

from __future__ import annotations

import time
from copy import deepcopy

from codex_mgr.core.adapters.responses import adapt_response, identifier, response_usage
from codex_mgr.core.adapters.sse import json_events
from codex_mgr.core.adapters.tools import ProtocolError, object_json, text

_MAX_ITEMS = 4096


def _index(value):
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value < _MAX_ITEMS:
        raise ProtocolError("Invalid upstream content index.")
    return value


def _fields(value, allowed):
    if not isinstance(value, dict) or any(
        key not in allowed and item is not None for key, item in value.items()
    ):
        raise ProtocolError("Upstream content needs a verified stream mapping.")


class _Lifecycle:
    def __init__(self, adapted):
        self.response = {
            "id": identifier("resp_"),
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "model": adapted.model,
            "output": [],
            "error": None,
            "incomplete_details": None,
            "usage": None,
        }
        self.sequence = 0
        self.messages = {}

    def event(self, kind, **fields):
        result = {"type": kind, "sequence_number": self.sequence, **fields}
        self.sequence += 1
        return result

    def start(self):
        return self.event("response.created", response=deepcopy(self.response))

    def delta(self, output_index, content_index, value):
        value = text(value)
        if output_index not in self.messages:
            item = {
                "id": identifier("msg_"),
                "type": "message",
                "role": "assistant",
                "status": "in_progress",
                "content": [],
            }
            self.messages[output_index] = item
            yield self.event(
                "response.output_item.added", output_index=output_index, item=deepcopy(item)
            )
        item = self.messages[output_index]
        if content_index == len(item["content"]):
            part = {"type": "output_text", "text": "", "annotations": []}
            item["content"].append(part)
            yield self.event(
                "response.content_part.added",
                output_index=output_index,
                content_index=content_index,
                item_id=item["id"],
                part=deepcopy(part),
            )
        elif not 0 <= content_index < len(item["content"]):
            raise ProtocolError("Invalid translated content index.")
        if value:
            yield self.event(
                "response.output_text.delta",
                output_index=output_index,
                content_index=content_index,
                item_id=item["id"],
                delta=value,
            )

    def finish(self, response):
        response["id"] = self.response["id"]
        response["created_at"] = self.response["created_at"]
        for index, item in enumerate(response["output"]):
            if item["type"] == "message":
                live = self.messages.get(index)
                if live is None or len(live["content"]) != len(item["content"]):
                    raise ProtocolError("Translated message lifecycle is inconsistent.")
                item["id"] = live["id"]
            # Validate all lifecycle identities before publishing any tool completion.
        events = []
        for index, item in enumerate(response["output"]):
            if item["type"] == "message":
                for part_index, part in enumerate(item["content"]):
                    fields = {
                        "output_index": index,
                        "content_index": part_index,
                        "item_id": item["id"],
                    }
                    events.append(
                        self.event("response.output_text.done", **fields, text=part["text"])
                    )
                    events.append(
                        self.event("response.content_part.done", **fields, part=deepcopy(part))
                    )
            else:
                added = deepcopy(item)
                key = "arguments" if item["type"] == "function_call" else "input"
                value = added[key]
                added[key] = ""
                if "status" in added:
                    added["status"] = "in_progress"
                events.append(
                    self.event("response.output_item.added", output_index=index, item=added)
                )
                prefix = (
                    "response.function_call_arguments"
                    if key == "arguments"
                    else "response.custom_tool_call_input"
                )
                fields = {"output_index": index, "item_id": item["id"], "call_id": item["call_id"]}
                if value:
                    events.append(self.event(prefix + ".delta", **fields, delta=value))
                events.append(self.event(prefix + ".done", **fields, **{key: value}))
            events.append(
                self.event("response.output_item.done", output_index=index, item=deepcopy(item))
            )
        events.append(self.event("response." + response["status"], response=response))
        return events

    def failed(self):
        response = deepcopy(self.response)
        response.update(
            status="failed",
            error={
                "code": "adapter_stream_error",
                "message": "The upstream stream could not be validated.",
            },
        )
        return self.event("response.failed", response=response)


class _Chat:
    def __init__(self, adapted, lifecycle):
        self.adapted, self.lifecycle = adapted, lifecycle
        self.identity = None
        self.model = None
        self.finish_reason = None
        self.done = False
        self.usage = None
        self.content, self.refusal, self.calls = [], [], []
        self.parts = []

    def accept(self, event):
        if self.done:
            raise ProtocolError("Upstream sent content after its terminal event.")
        if event.done:
            if self.finish_reason is None:
                raise ProtocolError("Upstream ended before its finish state.")
            self.done = True
            return []
        body = event.data
        if event.name not in {None, "message", "chat.completion.chunk"}:
            raise ProtocolError("Unsupported upstream SSE event type.")
        _fields(
            body,
            {
                "id",
                "object",
                "created",
                "model",
                "choices",
                "usage",
                "system_fingerprint",
                "service_tier",
            },
        )
        if body.get("object") != "chat.completion.chunk":
            raise ProtocolError("Invalid upstream Chat stream object.")
        identity, model = (
            text(body.get("id"), nonempty=True),
            text(body.get("model"), nonempty=True),
        )
        if self.identity is None:
            self.identity, self.model = identity, model
        elif (identity, model) != (self.identity, self.model):
            raise ProtocolError("Upstream changed its stream identity.")
        usage = body.get("usage")
        if usage is not None:
            choices = body.get("choices")
            final_choice = (
                isinstance(choices, list)
                and len(choices) == 1
                and isinstance(choices[0], dict)
                and choices[0].get("finish_reason") is not None
            )
            if self.finish_reason is None and not final_choice:
                raise ProtocolError("Upstream reported final usage before its finish state.")
            if self.usage is not None:
                raise ProtocolError("Upstream repeated final token usage.")
            response_usage(usage, "chat")
            self.usage = usage
        choices = body.get("choices")
        if choices == [] and usage is not None and self.finish_reason is not None:
            return []
        if self.finish_reason is not None:
            raise ProtocolError("Upstream sent content after its finish state.")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProtocolError("The adapter requires one upstream completion choice.")
        choice = choices[0]
        _fields(choice, {"index", "delta", "finish_reason", "logprobs"})
        if _index(choice.get("index")) != 0 or choice.get("logprobs"):
            raise ProtocolError("Unsupported upstream choice metadata.")
        delta = choice.get("delta")
        _fields(delta, {"role", "content", "refusal", "tool_calls"})
        if delta.get("role") not in {None, "assistant"}:
            raise ProtocolError("Invalid upstream stream role.")
        events = []
        for key, values in (("content", self.content), ("refusal", self.refusal)):
            if delta.get(key) is not None:
                value = text(delta[key])
                if key not in self.parts:
                    self.parts.append(key)
                # Buffered Chat output uses content then refusal. Switching back
                # to content after refusal cannot preserve that content lifecycle.
                if self.parts != [part for part in ("content", "refusal") if part in self.parts]:
                    raise ProtocolError("Upstream reordered its text and refusal parts.")
                values.append(value)
                events.extend(self.lifecycle.delta(0, self.parts.index(key), value))
        calls = delta.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list):
                raise ProtocolError("Invalid upstream tool fragments.")
            for call in calls:
                _fields(call, {"index", "id", "type", "function"})
                index = _index(call.get("index"))
                if index == len(self.calls):
                    self.calls.append({"id": None, "name": [], "arguments": []})
                elif index > len(self.calls):
                    raise ProtocolError("Upstream tool indexes are not contiguous.")
                stored = self.calls[index]
                if call.get("type") not in {None, "function"}:
                    raise ProtocolError("Unsupported upstream tool fragment type.")
                if call.get("id") is not None:
                    identity = text(call["id"], nonempty=True)
                    if stored["id"] is not None and identity != stored["id"]:
                        raise ProtocolError("Upstream changed a tool call ID.")
                    stored["id"] = identity
                function = call.get("function")
                if function is not None:
                    _fields(function, {"name", "arguments"})
                    for key in ("name", "arguments"):
                        if function.get(key) is not None:
                            stored[key].append(text(function[key]))
        finish = choice.get("finish_reason")
        if finish is not None:
            if finish not in {"stop", "tool_calls", "length", "content_filter"}:
                raise ProtocolError("Unsupported upstream finish reason.")
            self.finish_reason = finish
        return events

    def complete(self):
        if not self.done:
            raise ProtocolError("Upstream omitted its terminal sentinel.")
        message = {"role": "assistant"}
        if "content" in self.parts:
            message["content"] = "".join(self.content)
        if "refusal" in self.parts:
            message["refusal"] = "".join(self.refusal)
        if self.calls:
            message["tool_calls"] = [
                {
                    "id": call["id"],
                    "type": "function",
                    "function": {
                        "name": "".join(call["name"]),
                        "arguments": "".join(call["arguments"]),
                    },
                }
                for call in self.calls
            ]
        return adapt_response(
            {
                "model": self.model,
                "choices": [{"message": message, "finish_reason": self.finish_reason}],
                "usage": self.usage,
            },
            self.adapted,
        )


class _Messages:
    def __init__(self, adapted, lifecycle):
        self.adapted, self.lifecycle = adapted, lifecycle
        self.message = None
        self.blocks = []
        self.active = None
        self.done = False
        self.finish_reason = None

    def accept(self, event):
        body = event.data
        if self.done or event.done:
            raise ProtocolError("Invalid upstream Messages terminal event.")
        kind = body.get("type")
        if event.name is not None and event.name != kind:
            raise ProtocolError("Upstream SSE event name conflicts with its body.")
        if kind == "ping":
            _fields(body, {"type"})
            return []
        if kind == "message_start":
            _fields(body, {"type", "message"})
            message = body.get("message")
            _fields(
                message,
                {"id", "type", "role", "model", "content", "stop_reason", "stop_sequence", "usage"},
            )
            if (
                self.message is not None
                or message.get("type") != "message"
                or message.get("role") != "assistant"
                or message.get("content") != []
                or message.get("stop_reason") is not None
            ):
                raise ProtocolError("Invalid upstream message start.")
            text(message.get("id"), nonempty=True)
            text(message.get("model"), nonempty=True)
            response_usage(message.get("usage"), "messages")
            self.message = deepcopy(message)
            return []
        if self.message is None:
            raise ProtocolError("Upstream omitted its message start.")
        if kind == "content_block_start":
            _fields(body, {"type", "index", "content_block"})
            index = _index(body.get("index"))
            if (
                self.finish_reason is not None
                or self.active is not None
                or index != len(self.blocks)
            ):
                raise ProtocolError("Invalid upstream content block order.")
            block = body.get("content_block")
            if not isinstance(block, dict):
                raise ProtocolError("Invalid upstream content block.")
            if block.get("type") == "text":
                _fields(block, {"type", "text", "citations"})
                if block.get("citations"):
                    raise ProtocolError("Upstream citations need a verified stream mapping.")
                value = text(block.get("text"))
                self.blocks.append({"type": "text", "parts": [value]})
                events = list(self.lifecycle.delta(index, 0, value))
            elif block.get("type") == "tool_use":
                _fields(block, {"type", "id", "name", "input"})
                text(block.get("id"), nonempty=True)
                text(block.get("name"), nonempty=True)
                if not isinstance(block.get("input"), dict):
                    raise ProtocolError("Invalid upstream tool block input.")
                self.blocks.append({**block, "parts": []})
                events = []
            else:
                raise ProtocolError("This upstream block needs a verified stream mapping.")
            self.active = index
            return events
        if kind in {"content_block_delta", "content_block_stop"}:
            _fields(
                body, {"type", "index", "delta"} if kind.endswith("delta") else {"type", "index"}
            )
            index = _index(body.get("index"))
            if self.active != index:
                raise ProtocolError("Upstream delta or stop has no matching active block.")
            block = self.blocks[index]
            if kind == "content_block_stop":
                if block["type"] == "tool_use" and block["parts"]:
                    block["input"] = object_json("".join(block["parts"]))
                self.active = None
                return []
            delta = body.get("delta")
            if block["type"] == "text":
                _fields(delta, {"type", "text"})
                if delta.get("type") != "text_delta":
                    raise ProtocolError("Invalid upstream text delta type.")
                value = text(delta.get("text"))
                block["parts"].append(value)
                return list(self.lifecycle.delta(index, 0, value))
            _fields(delta, {"type", "partial_json"})
            if delta.get("type") != "input_json_delta" or block["input"]:
                raise ProtocolError("Invalid or ambiguous upstream tool delta.")
            block["parts"].append(text(delta.get("partial_json")))
            return []
        if kind == "message_delta":
            _fields(body, {"type", "delta", "usage"})
            if self.active is not None or self.finish_reason is not None:
                raise ProtocolError("Invalid upstream message delta order.")
            delta = body.get("delta")
            _fields(delta, {"stop_reason", "stop_sequence", "stop_details"})
            if delta.get("stop_details") is not None:
                raise ProtocolError("Upstream stop details need a verified mapping.")
            finish = delta.get("stop_reason")
            if finish is not None and finish not in {
                "end_turn",
                "stop_sequence",
                "tool_use",
                "max_tokens",
                "refusal",
            }:
                raise ProtocolError("Unsupported upstream stop reason.")
            sequence = delta.get("stop_sequence")
            if sequence is not None:
                text(sequence)
            usage = body.get("usage")
            _fields(
                usage,
                {
                    "input_tokens",
                    "output_tokens",
                    "cache_read_input_tokens",
                    "cache_creation_input_tokens",
                },
            )
            original = self.message.get("usage") or {}
            merged = {**original, **usage}
            response_usage(merged, "messages")
            if any(value < original.get(key, 0) for key, value in usage.items()):
                raise ProtocolError("Upstream cumulative token usage decreased.")
            self.message["usage"] = merged
            self.message["stop_reason"] = finish
            self.message["stop_sequence"] = sequence
            self.finish_reason = finish
            return []
        if kind == "message_stop":
            _fields(body, {"type"})
            if self.active is not None or self.finish_reason is None:
                raise ProtocolError("Upstream stopped before its complete finish state.")
            self.done = True
            return []
        raise ProtocolError("Unsupported upstream Messages stream event.")

    def complete(self):
        if not self.done:
            raise ProtocolError("Upstream omitted its message stop.")
        content = []
        for block in self.blocks:
            if block["type"] == "text":
                content.append({"type": "text", "text": "".join(block["parts"])})
            else:
                content.append({key: value for key, value in block.items() if key != "parts"})
        self.message["content"] = content
        return adapt_response(self.message, self.adapted)


def adapt_stream(chunks, adapted, **limits):
    """Yield one lifecycle with a fixed failed terminal on malformed upstream data.

    A successful terminal follows upstream EOF, so trailing corruption cannot
    publish an executable tool completion. The caller owns upstream cancellation.
    """
    lifecycle = _Lifecycle(adapted)
    yield lifecycle.start()
    events = json_events(chunks, **limits)
    try:
        if adapted.protocol not in {"chat", "messages"}:
            raise ProtocolError("Unsupported upstream stream protocol.")
        state = (
            _Chat(adapted, lifecycle)
            if adapted.protocol == "chat"
            else _Messages(adapted, lifecycle)
        )
        for event in events:
            yield from state.accept(event)
        final_events = lifecycle.finish(state.complete())
    except (ProtocolError, OSError, ValueError, TypeError, RecursionError, OverflowError):
        yield lifecycle.failed()
    else:
        yield from final_events
    finally:
        events.close()
