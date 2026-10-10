"""Reversible function/custom tool identities for flat upstream tool protocols."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass, field


class ProtocolError(ValueError):
    """Fixed diagnostic text only: never include request or upstream content."""


def text(value, message="Invalid protocol text.", *, nonempty=False):
    if not isinstance(value, str) or (nonempty and not value.strip()) or "\x00" in value:
        raise ProtocolError(message)
    return value


def object_json(value):
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ProtocolError("Tool arguments contain duplicate fields.")
            result[key] = item
        return result

    def no_constant(_):
        raise ProtocolError("Invalid tool arguments.")

    try:
        parsed = json.loads(
            text(value),
            object_pairs_hook=unique,
            parse_constant=no_constant,
        )
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProtocolError("Tool arguments must be a JSON object.") from exc
    if not isinstance(parsed, dict):
        raise ProtocolError("Tool arguments must be a JSON object.")
    return parsed


def encoded_json(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ProtocolError("Invalid protocol JSON.") from exc


@dataclass(frozen=True)
class ToolBinding:
    name: str
    namespace: str | None
    custom: bool
    wire_name: str

    def call(self, call_id, arguments):
        """Decode an upstream call without accepting undeclared tool identity."""
        call_id = text(call_id, "Invalid tool call ID.", nonempty=True)
        values = object_json(arguments)
        result = {"call_id": call_id, "name": self.name}
        if self.namespace is not None:
            result["namespace"] = self.namespace
        if self.custom:
            if set(values) != {"input"} or not isinstance(values["input"], str):
                raise ProtocolError("Custom tool arguments need one input string.")
            result.update(type="custom_tool_call", input=values["input"])
        else:
            result.update(type="function_call", arguments=arguments)
        return result


@dataclass
class ToolRegistry:
    bindings: dict[tuple[str | None, str], ToolBinding] = field(default_factory=dict, repr=False)
    declared: dict[str, ToolBinding] = field(default_factory=dict, repr=False)
    aliases: dict[str, ToolBinding] = field(default_factory=dict, repr=False)
    definitions: list[dict] = field(default_factory=list, repr=False)

    def binding(self, name, namespace=None, *, custom=False, historical=False):
        name = text(name, "Invalid tool name.", nonempty=True)
        if namespace is not None:
            namespace = text(namespace, "Invalid tool namespace.", nonempty=True)
        key = (None if namespace == "functions" else namespace, name)
        binding = self.bindings.get(key)
        if binding is not None:
            if not historical and binding.wire_name not in self.declared:
                raise ProtocolError("Tool choice references an undeclared tool.")
            if binding.custom != custom:
                raise ProtocolError("Tool history conflicts with its declared type.")
            return binding
        if not historical:
            raise ProtocolError("Tool choice references an undeclared tool.")
        digest = hashlib.sha256(encoded_json([namespace, name]).encode()).hexdigest()
        wire = (
            name
            if namespace is None
            and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name)
            and not name.startswith("cmgr_")
            else "cmgr_" + digest[:32]
        )
        binding = ToolBinding(name, namespace, custom, wire)
        if wire in self.aliases:
            raise ProtocolError("Tool names collide after translation.")
        self.bindings[key] = binding
        self.aliases[wire] = binding
        return binding

    def upstream_call(self, wire_name, call_id, arguments):
        wire_name = text(wire_name, "Invalid upstream tool name.", nonempty=True)
        binding = self.declared.get(wire_name)
        if binding is None:
            raise ProtocolError("Upstream called an undeclared tool.")
        return binding.call(call_id, arguments)

    def add(self, tool, namespace=None, namespace_description=None):
        if not isinstance(tool, dict):
            raise ProtocolError("Invalid tool definition.")
        kind = tool.get("type")
        if kind == "namespace" and namespace is None:
            group = text(tool.get("name"), "Invalid tool namespace.", nonempty=True)
            nested = tool.get("tools")
            if not isinstance(nested, list) or not nested:
                raise ProtocolError("A tool namespace needs tool definitions.")
            description = tool.get("description")
            if description is not None:
                text(description)
            for item in nested:
                self.add(item, group, description)
            return
        if kind not in {"function", "custom"}:
            raise ProtocolError("This tool type has no verified adapter mapping.")
        if tool.get("defer_loading") or tool.get("allowed_callers"):
            raise ProtocolError("Deferred or programmatic tool calls need a verified mapping.")
        binding = self.binding(
            tool.get("name"), namespace, custom=kind == "custom", historical=True
        )
        if binding.wire_name in self.declared:
            raise ProtocolError("Duplicate tool definitions are ambiguous.")
        self.declared[binding.wire_name] = binding
        description = text(tool.get("description") or "")
        if namespace_description:
            description = namespace_description + "\n" + description
        if kind == "function":
            parameters = tool.get("parameters") or {"type": "object", "properties": {}}
            if not isinstance(parameters, dict) or parameters.get("type") != "object":
                raise ProtocolError("A function tool needs an object schema.")
        else:
            parameters = {
                "type": "object",
                "properties": {"input": {"type": "string"}},
                "required": ["input"],
                "additionalProperties": False,
            }
            format_ = tool.get("format") or {"type": "text"}
            if not isinstance(format_, dict):
                raise ProtocolError("Invalid custom tool format.")
            if format_.get("type") == "grammar" and format_.get("syntax") in {"lark", "regex"}:
                grammar = text(
                    format_.get("definition"), "Invalid custom tool grammar.", nonempty=True
                )
                description += "\nThe input string must follow this grammar:\n" + grammar
            elif format_.get("type") != "text":
                raise ProtocolError("This custom tool format has no verified mapping.")
        function = {
            "name": binding.wire_name,
            "description": description,
            "parameters": deepcopy(parameters),
        }
        if tool.get("strict") is not None:
            if not isinstance(tool["strict"], bool):
                raise ProtocolError("Invalid function strictness.")
            function["strict"] = tool["strict"]
        self.definitions.append(function)

    @classmethod
    def from_tools(cls, tools):
        registry = cls()
        if tools is None:
            return registry
        if not isinstance(tools, list):
            raise ProtocolError("Invalid tool definitions.")
        for tool in tools:
            registry.add(tool)
        return registry
