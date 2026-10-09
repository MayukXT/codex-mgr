"""Catalog identity, real capabilities, credential routing and paging contracts."""

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

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
