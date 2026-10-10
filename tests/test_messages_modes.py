"""Mode, effort and conditional Messages constraints preserve coding history."""

from copy import deepcopy

import httpx
import pytest
from test_adapter_runtime import headers, payload, synthetic_loopback_http, upstream  # noqa: F401 -- fixture
from test_continuations import SIGNED, codec
from test_protocol_codecs import FUNCTION, messages_reply, request

from codex_mgr.config.schema import ModelInfo
from codex_mgr.core.adapters.launch import AdapterLaunch
from codex_mgr.core.adapters.messages_controls import EFFORTS, MODES
from codex_mgr.core.adapters.requests import adapt_request
from codex_mgr.core.adapters.responses import adapt_response
from codex_mgr.core.adapters.runtime import AdapterRuntime
from codex_mgr.core.adapters.tools import ProtocolError
from codex_mgr.core.models import model_metadata, select_model


def metadata(identifier="exact/router:model", **values):
    return ModelInfo(
        **{
            "id": identifier,
            "name": "Synthetic",
            "is_custom": True,
            "thinking_types": dict.fromkeys(MODES, True),
            "effort_supported": True,
            "supported_reasoning_levels": list(EFFORTS),
            "max_output_tokens": 4096,
            "disabled_high_effort_supported": True,
            **values,
        }
    )


def translate(mode, *, effort="medium", info=None, summary="none", **values):
    return adapt_request(
        request(reasoning={"effort": effort, "summary": summary}, **values),
        "messages",
        thinking_mode=mode,
        thinking_budget=1024 if mode == "enabled" else None,
        model_info=info or metadata(),
        continuations=codec(),
    )


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("effort", [None, "none", *EFFORTS])
def test_mode_and_effort_are_independent_without_guessed_budgets(mode, effort):
    if mode == "between_tools" and effort in {"xhigh", "max"}:
        with pytest.raises(ProtocolError, match="low, medium or high"):
            translate(mode, effort=effort)
        return
    adapted = translate(mode, effort=effort)
    assert adapted.payload["thinking"]["type"] == mode
    assert ("budget_tokens" in adapted.payload["thinking"]) == (mode == "enabled")
    assert ("output_config" in adapted.payload) == (effort not in {None, "none"})
    if effort not in {None, "none"}:
        assert adapted.payload["output_config"] == {"effort": effort}
    if mode in {"disabled", "between_tools"}:
        assert adapted.payload["thinking"] == {"type": mode}
    assert adapted.payload["max_tokens"] == 4096


@pytest.mark.parametrize("mode", ["adaptive", "enabled", "between_tools"])
@pytest.mark.parametrize("summary", ["none", "auto"])
def test_each_active_mode_round_trips_exact_signed_blocks_and_summary_preference(mode, summary):
    original = translate(mode, summary=summary)
    body = messages_reply([SIGNED, {"type": "text", "text": "Done."}])
    output = adapt_response(body, original)["output"]
    assert output[0]["summary"] == (
        [{"type": "summary_text", "text": SIGNED["thinking"]}] if summary == "auto" else []
    )
    value = request(
        reasoning={"effort": "high", "summary": summary},
        input=[
            {"role": "user", "content": request()["input"]},
            *output,
            {"role": "user", "content": "Continue."},
        ],
    )
    before = deepcopy(value)
    replay = adapt_request(
        value,
        "messages",
        thinking_mode=mode,
        thinking_budget=1024 if mode == "enabled" else None,
        model_info=metadata(),
        continuations=codec(),
    )
    assert replay.payload["messages"][1]["content"] == body["content"] and value == before
    assert replay.payload["output_config"] == {"effort": "high"}


@pytest.mark.parametrize("mode", MODES)
def test_negative_capability_and_output_ceiling_stop_translation(mode):
    with pytest.raises(ProtocolError, match="thinking mode"):
        translate(mode, info=metadata(thinking_types={mode: False}))
    with pytest.raises(ProtocolError, match="advertise"):
        translate(mode, info=metadata(effort_supported=False))
    with pytest.raises(ProtocolError, match="ceiling"):
        translate(mode, max_output_tokens=4097)
    with pytest.raises(ProtocolError, match="another model"):
        translate(mode, info=metadata(identifier="other"))


@pytest.mark.parametrize("mode", ["adaptive", "disabled", "between_tools"])
def test_modern_modes_do_not_accept_manual_budgets(mode):
    with pytest.raises(ProtocolError, match="manual budget"):
        adapt_request(request(), "messages", thinking_mode=mode, thinking_budget=1024)


@pytest.mark.parametrize(
    "identifier,permitted",
    [
        ("claude-opus-5-5", False),
        ("claude-sonnet-5-5", False),
        ("claude-fable-5-1", False),
        ("claude-mythos-5-1", False),
        ("claude-opus-5", True),
        ("claude-haiku-5-5", True),
        ("claude-opus-4-6", True),
        ("gateway/claude-opus-5", False),
    ],
)
def test_forced_choice_uses_exact_reviewed_dialect_or_explicit_constraints(identifier, permitted):
    info = metadata(identifier=identifier)
    options = {"model": identifier, "tools": [FUNCTION], "tool_choice": "required"}
    if permitted:
        assert translate("adaptive", info=info, **options).payload["tool_choice"] == {"type": "any"}
    else:
        with pytest.raises(ProtocolError, match="Forced tools"):
            translate("adaptive", info=info, **options)
    info.forced_tools_supported = True
    assert translate("adaptive", info=info, **options).payload["tool_choice"] == {"type": "any"}
    with pytest.raises(ProtocolError, match="Forced tools"):
        translate("enabled", info=info, **options)


@pytest.mark.parametrize(
    "identifier,allowed",
    [
        ("claude-opus-5", False),
        ("claude-haiku-5-5", False),
        ("claude-sonnet-5", True),
    ],
)
def test_disabled_higher_effort_needs_a_compatible_reviewed_constraint(identifier, allowed):
    info = metadata(identifier=identifier, disabled_high_effort_supported=None)
    if allowed:
        assert translate("disabled", effort="max", info=info, model=identifier).payload[
            "output_config"
        ] == {"effort": "max"}
    else:
        with pytest.raises(ProtocolError, match="low, medium or high"):
            translate("disabled", effort="max", info=info, model=identifier)


@pytest.mark.parametrize("mode", ["adaptive", "disabled"])
@pytest.mark.parametrize(
    "values,allowed",
    [
        ({"temperature": 1}, True),
        ({"top_p": 0.99}, True),
        ({"temperature": 0.5}, False),
        ({"top_p": 1}, False),
        ({"temperature": 1, "top_p": 0.99}, False),
    ],
)
def test_modern_default_sampling_restrictions_apply_even_when_thinking_is_off(
    mode, values, allowed
):
    info = metadata(identifier="claude-haiku-5-5")
    if allowed:
        result = translate(mode, info=info, model=info.id, **values).payload
        assert all(result[key] == value for key, value in values.items())
    else:
        with pytest.raises(ProtocolError, match="default sampling"):
            translate(mode, info=info, model=info.id, **values)


def test_unknown_aliases_and_stale_capabilities_cannot_select_reviewed_sampling_rules():
    with pytest.raises(ProtocolError, match="reviewed Messages"):
        translate("adaptive", top_p=0.99)
    with pytest.raises(ProtocolError, match="reviewed Messages"):
        translate(
            "adaptive",
            info=metadata(identifier="claude-opus-5-5", thinking_types={}),
            model="claude-opus-5-5",
            top_p=0.99,
        )
    assert (
        translate("adaptive", info=metadata(sampling_default_only=True), top_p=0.99).payload[
            "top_p"
        ]
        == 0.99
    )


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("stream", [False, True])
def test_actual_http_mode_effort_and_inactive_budget_match_saved_controls(
    mode, stream, monkeypatch
):
    import json

    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    raw = json.dumps(messages_reply()).encode()
    with upstream("messages", buffered=True, raw=raw) as (provider, received, _):
        provider.messages_thinking, provider.reasoning_budget, provider.streaming = (
            mode,
            1024,
            False,
        )
        with AdapterRuntime(provider) as runtime:
            value = payload(stream=stream)
            value["reasoning"] = {"effort": "medium"}
            response = httpx.post(
                runtime.base_url + "/responses",
                headers=headers(runtime),
                json=value,
                trust_env=False,
            )
            assert response.status_code == 200 and len(received) == 1
            sent = received[0][2]
            assert sent["thinking"]["type"] == mode and sent["output_config"] == {
                "effort": "medium"
            }
            assert ("budget_tokens" in sent["thinking"]) == (mode == "enabled")
        assert provider.reasoning_budget == 1024


def test_known_output_ceiling_can_reduce_the_default_without_guessing_capacity():
    assert (
        translate("adaptive", info=metadata(max_output_tokens=2048)).payload["max_tokens"] == 2048
    )


def test_model_selection_uses_required_adaptive_flags_but_preserves_explicit_off_or_stale_choices():
    from codex_mgr.config.schema import ProviderConfig

    provider = ProviderConfig(
        id="fixture",
        name="Synthetic",
        base_url="https://fixture.invalid/v1",
        wire_api="anthropic_proxy",
        models=[metadata(thinking_types={"adaptive": True, "enabled": False, "disabled": False})],
    )
    select_model(provider, "exact/router:model")
    assert provider.messages_thinking == "adaptive"
    provider.messages_thinking, provider.reasoning_enabled = None, False
    select_model(provider, "exact/router:model")
    assert provider.messages_thinking is None
    provider.models[0].is_custom = False
    provider.reasoning_enabled = True
    assert model_metadata(provider, "exact/router:model").thinking_types == {}
    select_model(provider, "exact/router:model")
    assert provider.messages_thinking is None


def test_disabled_summary_and_foreign_protocol_modes_are_rejected_without_key_creation():
    with pytest.raises(ProtocolError, match="active Messages"):
        translate("disabled", summary="auto")
    with pytest.raises(ProtocolError, match="Messages connection"):
        adapt_request(request(), "chat", thinking_mode="adaptive")
    with pytest.raises(ProtocolError, match="active, bound"):
        adapt_response(messages_reply([SIGNED]), translate("disabled"))


def test_pure_launch_keeps_inactive_budget_and_honors_effort_when_thinking_is_disabled():
    from codex_mgr.config.schema import ProviderConfig

    value = ProviderConfig(
        id="fixture",
        name="Synthetic",
        base_url="https://fixture.invalid/v1",
        wire_api="anthropic_proxy",
        messages_thinking="disabled",
        reasoning_effort="medium",
        reasoning_enabled=False,
        reasoning_budget=10000,
    )
    launch = AdapterLaunch(value)
    assert launch._effort == "medium" and value.reasoning_budget == 10000


@pytest.mark.parametrize("stale", [False, True])
def test_actual_runtime_uses_requested_model_capabilities_and_withholds_stale_flags(
    stale, monkeypatch
):
    import json

    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream("messages", buffered=True, raw=json.dumps(messages_reply()).encode()) as (
        provider,
        received,
        _,
    ):
        provider.messages_thinking, provider.streaming = "adaptive", False
        value = payload()
        identifier = value["model"] + "/alternate"
        value["model"] = identifier
        provider.models = [
            metadata(identifier=provider.default_model, thinking_types={"adaptive": True}),
            metadata(
                identifier=identifier, is_custom=not stale, thinking_types={"adaptive": False}
            ),
        ]
        with AdapterRuntime(provider) as runtime:
            response = httpx.post(
                runtime.base_url + "/responses",
                headers=headers(runtime),
                json=value,
                trust_env=False,
            )
        assert response.status_code == (200 if stale else 400)
        assert len(received) == int(stale)
        if stale:
            assert received[0][2]["max_tokens"] == 8192
