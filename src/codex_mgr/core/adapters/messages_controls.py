"""Messages mode/effort contracts and reviewed conditional model constraints.

Constraints not represented in aggregate model flags come from:
https://platform.claude.com/docs/en/build-with-claude/thinking (2026-10-11).
Exact advertised IDs are required; aliases/substrings do not identify a dialect.
"""

from codex_mgr.core.adapters.tools import ProtocolError

MODES = ("adaptive", "enabled", "disabled", "between_tools")
EFFORTS = ("low", "medium", "high", "xhigh", "max")
_DEFAULT_SAMPLING = {
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-haiku-5-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-opus-4-7",
    "claude-fable-5-1",
    "claude-mythos-5-1",
    "claude-fable-5",
    "claude-mythos-5",
    "claude-mythos-preview",
}
_NO_FORCED_TOOLS = {
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-fable-5-1",
    "claude-mythos-5-1",
}
_LEGACY_SAMPLING = {
    "claude-opus-4-6",
    "claude-sonnet-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-5",
    "claude-haiku-4-5",
}


def _constraints(model, mode):
    forced = sampling = higher = None
    if model is not None:
        # Fresh granular support is required before an ID selects reviewed rules.
        if (
            model.thinking_types.get(mode) is True
            and model.id in _DEFAULT_SAMPLING | _LEGACY_SAMPLING
        ):
            forced = model.id not in _NO_FORCED_TOOLS
            sampling = model.id in _DEFAULT_SAMPLING
            higher = model.id not in {"claude-opus-5", "claude-haiku-5-5"}
        for name, value in (
            ("forced", model.forced_tools_supported),
            ("sampling", model.sampling_default_only),
            ("higher", model.disabled_high_effort_supported),
        ):
            if value is not None:
                if name == "forced":
                    forced = value
                elif name == "sampling":
                    sampling = value
                else:
                    higher = value
    return forced, sampling, higher


def validate_model(model, requested_model, mode, effort, cap):
    if model is None:
        return
    if model.id != requested_model:
        raise ProtocolError("Messages capabilities belong to another model.")
    support = model.thinking_types.get(mode)
    if support is False or (
        support is None and mode != "disabled" and model.thinking_supported is False
    ):
        raise ProtocolError("The selected model does not support this Messages thinking mode.")
    if effort is not None and (
        model.effort_supported is False
        or (model.supported_reasoning_levels and effort not in model.supported_reasoning_levels)
    ):
        raise ProtocolError("The selected model does not advertise this Messages effort.")
    if model.max_output_tokens is not None and cap > model.max_output_tokens:
        raise ProtocolError("The output limit exceeds the selected model's reported ceiling.")


def apply_mode(payload, *, mode, effort, budget, summary, model):
    if mode not in MODES:
        raise ProtocolError("Invalid Messages thinking mode.")
    effort = None if effort in {None, "none"} else effort
    if effort is not None and effort not in EFFORTS:
        raise ProtocolError("Choose a supported Messages effort, independently of thinking mode.")
    if mode == "enabled":
        if (
            not isinstance(budget, int)
            or isinstance(budget, bool)
            or not 1024 <= budget < payload["max_tokens"]
        ):
            raise ProtocolError(
                "Manual Messages thinking needs a budget of at least 1024 below the output cap."
            )
        payload["thinking"] = {"type": mode, "budget_tokens": budget}
    else:
        if budget is not None:
            raise ProtocolError("This Messages thinking mode does not accept a manual budget.")
        payload["thinking"] = {"type": mode}
    validate_model(model, payload["model"], mode, effort, payload["max_tokens"])
    forced, _, higher = _constraints(model, mode)
    if effort in {"xhigh", "max"} and (
        mode == "between_tools" or (mode == "disabled" and higher is not True)
    ):
        raise ProtocolError(
            "This thinking mode needs low, medium or high effort; higher support is unverified or unavailable."
        )
    choice = payload.get("tool_choice", {}).get("type")
    if choice in {"any", "tool"} and (mode == "enabled" or forced is not True):
        raise ProtocolError(
            "Forced tools are unsupported or unverified for this Messages mode and model."
        )
    if effort is not None:
        payload["output_config"] = {"effort": effort}
    if summary == "auto" and mode == "disabled":
        raise ProtocolError("Readable summaries require supported active Messages thinking.")
    if mode in {"adaptive", "enabled"} and summary is not None:
        payload["thinking"]["display"] = "summarized" if summary == "auto" else "omitted"


def validate_sampling(request, *, mode, model):
    values = {key: request[key] for key in ("temperature", "top_p") if request.get(key) is not None}
    if not values:
        return
    _, defaults_only, _ = _constraints(model, mode)
    if defaults_only is None:
        raise ProtocolError("Sampling controls need reviewed Messages model constraints.")
    if defaults_only:
        if len(values) > 1 or any(
            value != (1 if key == "temperature" else 0.99) for key, value in values.items()
        ):
            raise ProtocolError("This Messages model accepts only one default sampling control.")
    elif mode != "disabled" and (
        values.get("temperature", 1) != 1 or values.get("top_p", 1) < 0.95
    ):
        raise ProtocolError(
            "Active Messages thinking requires default temperature and top_p from 0.95 to 1."
        )
