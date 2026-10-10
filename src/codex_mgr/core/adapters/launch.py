"""A verified, temporary native connection owned by one foreground launch."""

from __future__ import annotations

import hashlib
import os
import secrets
from pathlib import Path

import tomlkit

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.runtime import AdapterError, AdapterRuntime
from codex_mgr.core.adapters.tools import ProtocolError
from codex_mgr.core.models import model_metadata
from codex_mgr.core.native_config import (
    read_native_configuration,
    verify_after_activation,
    verify_before_activation,
)
from codex_mgr.core.profiles import ConfigurationError, connection_id
from codex_mgr.core.runner import launch_codex


def adapter_connection_id(provider):
    """Stable native identity; an existing definition is a conflict, never reused."""
    identity = provider.wire_api + "\0" + connection_id(provider)
    return "cmgr_adapter_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _controls(provider):
    modern = provider.wire_api == "anthropic_proxy" and provider.messages_thinking is not None
    effort = provider.reasoning_effort if modern or provider.reasoning_enabled else "none"
    if not modern and not provider.reasoning_enabled:
        provider.reasoning_budget = None
    if provider.wire_api == "anthropic_proxy" and not modern:
        if provider.reasoning_budget is None:
            if effort not in {None, "none"}:
                raise ConfigurationError("Messages reasoning needs an explicit thinking budget.")
            effort = "none"
        elif effort in {None, "none"}:
            raise ConfigurationError(
                "Choose an explicit native reasoning effort with this Messages thinking budget."
            )
    # Unknown/default native reasoning must not inherit an unrelated connection's effort.
    effort = effort or "none"
    model = model_metadata(provider, provider.default_model)
    if (
        model
        and model.supported_reasoning_levels
        and effort not in model.supported_reasoning_levels
        and effort != "none"
    ):
        raise ConfigurationError("The selected model does not advertise this reasoning effort.")
    try:
        adapt_request(
            {
                "model": provider.default_model,
                "input": "",
                "reasoning": {"effort": effort, "summary": provider.messages_summary},
            },
            "messages" if provider.wire_api == "anthropic_proxy" else "chat",
            max_output_tokens=provider.max_output_tokens,
            thinking_budget=provider.reasoning_budget
            if provider.wire_api != "anthropic_proxy"
            or provider.messages_thinking in {None, "enabled"}
            else None,
            thinking_mode=provider.messages_thinking
            if provider.wire_api == "anthropic_proxy"
            else None,
            model_info=model if provider.wire_api == "anthropic_proxy" else None,
        )
    except ProtocolError as exc:
        raise ConfigurationError(str(exc)) from exc
    return effort


class AdapterLaunch:
    """Keep upstream credentials in the owner and native routing in launch overrides.

    No settings or native configuration are written. Callers commit their durable
    selection only after this scope has verified the current policy and overrides.
    """

    def __init__(self, provider, *, cwd: Path | None = None):
        self._provider = ProviderConfig.model_validate(provider.model_dump())
        if not self._provider.model_selected:
            raise ConfigurationError("Choose an exact model ID before starting Codex.")
        if self._provider.wire_api not in {"chat", "anthropic_proxy"}:
            raise ConfigurationError("This connection does not need an adapter launch.")
        self._effort = _controls(self._provider)
        self._cwd = (cwd or Path.cwd()).resolve()
        self._runtime = None
        self._environment = None
        self._arguments = None
        self._entered = False

    def __enter__(self):
        if self._entered:
            raise AdapterError("An adapter launch cannot be started twice.")
        self._entered = True
        try:
            identity = adapter_connection_id(self._provider)
            before = read_native_configuration(cwd=self._cwd)
            if identity in before.config.get("model_providers", {}):
                raise ConfigurationError(
                    "An existing native provider conflicts with the adapter launch identity."
                )
            self._runtime = AdapterRuntime(self._provider)
            self._runtime.__enter__()
            variable = "CMGR_ADAPTER_TOKEN_" + secrets.token_hex(16).upper()
            self._environment = os.environ.copy()
            token = self._provider.auth_token
            if token and token.startswith("$"):
                self._environment.pop(token[1:], None)
            for name in (self._provider.env_http_headers or {}).values():
                self._environment.pop(name, None)
            self._environment[variable] = self._runtime.token
            table = {
                "name": "cmgr adapter",
                "base_url": self._runtime.base_url,
                "wire_api": "responses",
                "env_key": variable,
                "requires_openai_auth": False,
                "supports_websockets": False,
                "request_max_retries": 0,
                "stream_max_retries": 0,
            }
            document = {
                "model": self._provider.default_model,
                "model_provider": identity,
                "model_reasoning_effort": self._effort,
                "model_reasoning_summary": self._provider.messages_summary,
                "web_search": "disabled",
            }
            if self._provider.context_window_limit is not None:
                document["model_context_window"] = self._provider.context_window_limit
            values = list(document.items())
            inline = tomlkit.inline_table()
            inline.update(table)
            values.append(("model_providers." + identity, inline))
            self._arguments = []
            for key, value in values:
                self._arguments.extend(["-c", key + "=" + tomlkit.item(value).as_string()])
            document["model_providers"] = {identity: table}
            encoded = tomlkit.dumps(document).encode("utf-8")
            verify_before_activation(encoded, cwd=self._cwd, snapshot=before)
            verify_after_activation(
                encoded,
                cwd=self._cwd,
                overrides=self._arguments[1::2],
                environment=self._environment,
            )
            return self
        except BaseException:
            self.close()
            raise

    @property
    def arguments(self):
        self._require_live()
        return list(self._arguments)

    @property
    def environment(self):
        self._require_live()
        return dict(self._environment)

    def _require_live(self):
        if self._environment is None or self._runtime is None:
            raise AdapterError("The adapter launch is not running.")
        _ = self._runtime.base_url

    def run(self, *, session_id=None, prompt=None):
        return launch_codex(
            session_id=session_id,
            prompt=prompt,
            extra_args=self.arguments + ["-C", str(self._cwd)],
            environment=self.environment,
        )

    def close(self):
        if self._environment is not None:
            self._environment.clear()
            self._environment = None
        self._arguments = None
        if self._runtime is not None:
            self._runtime.close()

    def __exit__(self, *_):
        self.close()
