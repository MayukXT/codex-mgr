"""Catalog identity, real capabilities, credential routing and paging contracts."""

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from codex_mgr.config.schema import ModelInfo, ProviderConfig
from codex_mgr.core import models


def provider(**values):
    return ProviderConfig(id="example", name="Example", base_url="https://api.example/v1", **values)


def client_fixture(monkeypatch, handler):
    original = httpx.Client
    monkeypatch.setattr(
        models.httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )


def test_unknown_model_capabilities_are_not_guessed():
    for identifier in ("contains-6", "claude-model", "anything"):
        item = models.parse_model({"id": identifier})
        assert item.context_window is None
        assert item.supported_reasoning_levels == []
        assert item.reasoning_supported is None
    assert ModelInfo(id="manual", name="Manual").context_window is None


def test_reported_metadata_and_visibility_are_preserved():
    result = models._models(
        [
            {
                "model": "exact/router:id",
                "displayName": "Named model",
                "context_window": 8192,
                "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
                "defaultReasoningEffort": "high",
            },
            {"id": "hidden", "hidden": True},
            {"id": "retired", "lifecycle": "retired"},
            {"id": "bad\ntext"},
        ]
    )
    assert len(result) == 1
    assert result[0].id == "exact/router:id" and result[0].name == "Named model"
    assert result[0].context_window == 8192 and result[0].supported_reasoning_levels == ["high"]


def capability_record():
    return {
        "id": "exact/opaque-model-id",
        "display_name": "Reported model",
        "max_input_tokens": 96000,
        "max_tokens": 12000,
        "capabilities": {
            "thinking": {
                "supported": True,
                "types": {
                    "adaptive": {"supported": True},
                    "enabled": {"supported": False},
                    "disabled": {"supported": False},
                    "between_tools": {"supported": True},
                },
            },
            "effort": {
                "supported": True,
                "low": {"supported": True},
                "medium": {"supported": True},
                "high": {"supported": True},
                "xhigh": {"supported": False},
                "max": {"supported": True},
            },
        },
    }


def test_catalog_keeps_reported_thinking_modes_effort_and_output_ceiling():
    original = capability_record()
    before = deepcopy(original)
    item = models.parse_model(original)
    assert item.id == original["id"] and item.name == original["display_name"]
    assert item.context_window == 96000 and item.max_output_tokens == 12000
    assert item.thinking_supported is True and item.effort_supported is True
    assert item.thinking_types == {
        "adaptive": True,
        "enabled": False,
        "disabled": False,
        "between_tools": True,
    }
    assert item.supported_reasoning_levels == ["low", "medium", "high", "max"]
    assert original == before
    assert item.default_reasoning_effort is None


def test_disabled_type_support_is_independent_of_thinking_support():
    item = models.parse_model(
        {
            "id": "exact-no-thinking",
            "capabilities": {
                "thinking": {
                    "supported": False,
                    "types": {"disabled": {"supported": True}, "enabled": {"supported": False}},
                },
                "effort": {"supported": False},
            },
        }
    )
    assert item.thinking_supported is False and item.effort_supported is False
    assert item.thinking_types == {"disabled": True, "enabled": False}
    assert item.supported_reasoning_levels == []


@pytest.mark.parametrize("value", [None, 1, 0, "true", "false", [], {}])
def test_malformed_support_values_remain_unknown(value):
    record = capability_record()
    record["capabilities"]["thinking"]["supported"] = value
    record["capabilities"]["thinking"]["types"] = {"adaptive": {"supported": value}}
    record["capabilities"]["effort"] = {"supported": value, "high": {"supported": value}}
    item = models.parse_model(record)
    assert item.thinking_supported is None and item.effort_supported is None
    assert item.thinking_types == {} and item.supported_reasoning_levels == []


@pytest.mark.parametrize("value", [None, False, [], "private-reported-data", 1])
def test_malformed_capability_containers_do_not_break_or_certify_models(value):
    for capabilities in (
        value,
        {"thinking": value, "effort": value},
        {"thinking": {"types": value}},
    ):
        item = models.parse_model({"id": "exact", "capabilities": capabilities})
        assert item.thinking_types == {}
        assert item.thinking_supported is None and item.effort_supported is None
        assert "private-reported-data" not in repr(item)


@pytest.mark.parametrize("value", [None, False, True, 0, -1, "12000", 12.5])
def test_output_ceiling_is_not_guessed_from_invalid_values(value):
    item = models.parse_model({"id": "exact", "max_tokens": value})
    assert item.max_output_tokens is None


def test_unknown_capabilities_and_type_payloads_are_not_retained():
    record = capability_record()
    record["capabilities"]["private-note"] = "synthetic-private"
    record["capabilities"]["thinking"]["types"]["future-type"] = {"supported": True}
    record["capabilities"]["thinking"]["types"]["adaptive"]["private-note"] = "synthetic-private"
    item = models.parse_model(record)
    assert "synthetic-private" not in item.model_dump_json()
    assert "future-type" not in item.thinking_types
    restored = ModelInfo.model_validate_json(item.model_dump_json())
    assert restored == item


@pytest.mark.parametrize("field", ["thinking_supported", "effort_supported", "thinking_types"])
@pytest.mark.parametrize("value", [0, 1, "true"])
def test_persisted_support_flags_require_actual_booleans(field, value):
    supplied = {"adaptive": value} if field == "thinking_types" else value
    with pytest.raises(ValidationError):
        ModelInfo(id="exact", name="Exact", **{field: supplied})


def test_stale_capability_snapshot_keeps_only_labels(monkeypatch):
    monkeypatch.setenv("SELECTED_KEY", "synthetic-first")
    item = provider(auth_token="$SELECTED_KEY", models=[models.parse_model(capability_record())])
    item.catalog_source = "provider:/models"
    item.catalog_scope = models.catalog_scope(item)
    item.catalog_fetched_at = datetime.now(timezone.utc).isoformat()
    selected = models.model_metadata(item, item.models[0].id)
    assert selected.thinking_types["adaptive"] and selected.max_output_tokens == 12000
    monkeypatch.setenv("SELECTED_KEY", "synthetic-rotated")
    stale = models.model_metadata(item, selected.id)
    assert stale.id == selected.id and stale.name == selected.name
    assert stale.max_output_tokens is None and stale.thinking_types == {}
    assert stale.thinking_supported is None and stale.effort_supported is None


def test_anthropic_pagination_resolves_selected_key_and_endpoint(monkeypatch):
    requests = []
    monkeypatch.setenv("SELECTED_KEY", "synthetic-selected")

    def handler(request):
        requests.append(request)
        assert request.headers["x-api-key"] == "synthetic-selected"
        assert request.headers["anthropic-version"] == "2023-06-01"
        assert request.url.host == "api.example"
        if len(requests) == 1:
            return httpx.Response(
                200, json={"data": [{"id": "one"}], "has_more": True, "last_id": "one"}
            )
        assert request.url.params["after_id"] == "one"
        return httpx.Response(200, json={"data": [{"id": "two"}], "has_more": False})

    client_fixture(monkeypatch, handler)
    ok, values, _ = models.probe_remote_models(
        "https://api.example", "$SELECTED_KEY", "anthropic_proxy"
    )
    assert ok and [m.id for m in values] == ["one", "two"]
    assert requests[0].url.path == "/v1/models"


def test_catalog_cannot_forward_credentials_to_pagination_host(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(
            200, json={"data": [], "links": {"next": "https://other.example/models"}}
        )

    client_fixture(monkeypatch, handler)
    ok, _, error = models.probe_remote_models("https://api.example/v1", "synthetic-private")
    assert not ok and len(seen) == 1
    assert "synthetic-private" not in error


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 302])
def test_http_error_is_never_a_successful_connection(monkeypatch, status):
    client_fixture(
        monkeypatch, lambda request: httpx.Response(status, json={"error": "synthetic-private"})
    )
    ok, message, _ = models.test_provider_connectivity(provider(auth_token="synthetic-key"))
    assert not ok and "synthetic-private" not in message


def test_pagination_repetition_returns_failure_without_partial_catalog(monkeypatch):
    client_fixture(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"data": [{"id": "one"}], "has_more": True, "last_id": "one"}
        ),
    )
    ok, values, _ = models.probe_remote_models("https://api.example/v1")
    assert not ok and not values


def test_missing_selected_variable_prevents_network(monkeypatch):
    called = []
    client_fixture(monkeypatch, lambda request: called.append(request))
    assert not models.probe_remote_models("https://api.example/v1", "$MISSING_KEY")[0]
    assert not called


def test_catalog_freshness_changes_with_credential_rotation(monkeypatch):
    monkeypatch.setenv("SELECTED_KEY", "synthetic-first")
    item = provider(auth_token="$SELECTED_KEY")
    item.catalog_source = "provider:/models"
    item.catalog_scope = models.catalog_scope(item)
    item.catalog_fetched_at = datetime.now(timezone.utc).isoformat()
    assert models.catalog_is_fresh(item)
    monkeypatch.setenv("SELECTED_KEY", "synthetic-second")
    assert not models.catalog_is_fresh(item)


def test_local_cache_requires_identity_version_and_freshness(tmp_path):
    cache = {
        "identity": "synthetic-identity",
        "client_version": "0.159.0",
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "models": [{"slug": "exact", "display_name": "Exact", "context_window": 64000}],
    }
    (tmp_path / "unused").write_text("")
    path = models.get_models_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache))
    assert not models.sync_models_from_local_cache()
    assert not models.sync_models_from_local_cache(identity="other", client_version="0.159.0")
    assert (
        models.sync_models_from_local_cache(
            identity="synthetic-identity", client_version="0.159.0"
        )[0].context_window
        == 64000
    )
    cache["fetched_at"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    path.write_text(json.dumps(cache))
    assert not models.sync_models_from_local_cache(
        identity="synthetic-identity", client_version="0.159.0"
    )


def test_openai_id_does_not_override_selected_api_endpoint(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"data": []})

    client_fixture(monkeypatch, handler)
    item = ProviderConfig(
        id="openai", name="API", base_url="https://api.example/v1", auth_token="synthetic-key"
    )
    assert models.test_provider_connectivity(item)[0]
    assert seen[0].url.host == "api.example"
