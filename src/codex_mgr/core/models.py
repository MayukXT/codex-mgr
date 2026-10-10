"""Model catalogs and authentication checks; listing never proves inference support."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import httpx
from pydantic import ValidationError

from codex_mgr import __version__
from codex_mgr.config.schema import ModelInfo, ProviderConfig
from codex_mgr.core.auth import CredentialError, resolve_credential, verify_openai_connectivity
from codex_mgr.core.rpc import CodexReader, CodexRpcError
from codex_mgr.utils.paths import get_auth_json_path, get_models_cache_path


def _positive(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def parse_model(item: dict) -> ModelInfo:
    identifier = item.get("model") or item.get("slug") or item.get("id")
    levels = item.get("supportedReasoningEfforts", item.get("supported_reasoning_levels", []))
    efforts = []
    if isinstance(levels, list):
        for level in levels:
            effort = (
                (level.get("reasoningEffort") or level.get("effort"))
                if isinstance(level, dict)
                else level
            )
            if isinstance(effort, str) and effort and effort not in efforts:
                efforts.append(effort)
    capabilities = item.get("capabilities") or {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    effort_caps = capabilities.get("effort", {})
    if isinstance(effort_caps, dict):
        efforts.extend(
            key
            for key, value in effort_caps.items()
            if isinstance(value, dict) and value.get("supported") is True and key not in efforts
        )
    protocol = item.get("wire_api")
    if protocol not in {"responses", "chat", "anthropic_proxy"}:
        protocol = None
    return ModelInfo(
        id=identifier,
        name=item.get("displayName") or item.get("display_name") or item.get("name") or identifier,
        context_window=_positive(
            item.get("context_window") or item.get("context_length") or item.get("max_input_tokens")
        ),
        supported_reasoning_levels=efforts,
        default_reasoning_effort=item.get("defaultReasoningEffort")
        or item.get("default_reasoning_level"),
        reasoning_supported=bool(efforts)
        if "supportedReasoningEfforts" in item or "supported_reasoning_levels" in item
        else None,
        tools_supported=item.get("tools_supported")
        if isinstance(item.get("tools_supported"), bool)
        else None,
        input_modalities=item.get("inputModalities") or item.get("input_modalities") or [],
        wire_api=protocol,
    )


def _models(items):
    result = {}
    for item in items:
        if (
            not isinstance(item, dict)
            or item.get("hidden")
            or item.get("visibility") == "hide"
            or item.get("lifecycle") in {"retired", "deprecated"}
        ):
            continue
        try:
            model = parse_model(item)
        except (ValueError, TypeError, ValidationError):
            continue
        result[model.id] = model
    return list(result.values())


def sync_models_from_local_cache(
    *, identity=None, client_version=None, ttl=3600
) -> list[ModelInfo]:
    """Use a Codex cache only with an explicitly verified identity and version."""
    if not identity or not client_version:
        return []
    try:
        data = json.loads(get_models_cache_path().read_text(encoding="utf-8"))
        stamp = datetime.fromisoformat(data["fetched_at"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        if (
            data.get("identity") != identity
            or data.get("client_version") != client_version
            or not 0 <= age <= ttl
        ):
            return []
        return _models(data.get("models", []))
    except (OSError, ValueError, TypeError, KeyError):
        return []


def provider_headers(provider: ProviderConfig) -> dict[str, str]:
    headers = {"User-Agent": f"codex-mgr/{__version__}"}
    token = resolve_credential(provider.auth_token)
    if token:
        if provider.wire_api == "anthropic_proxy":
            headers["x-api-key"] = token
        else:
            headers["Authorization"] = f"Bearer {token}"
    if provider.wire_api == "anthropic_proxy":
        headers["anthropic-version"] = "2023-06-01"
    headers.update(provider.http_headers or {})
    for header, variable in (provider.env_http_headers or {}).items():
        value = resolve_credential("$" + variable)
        if value:
            headers[header] = value
    return headers


def catalog_scope(provider):
    """Opaque identity changes when the endpoint, transport or credentials change."""
    if provider.auth_type == "official_oauth":
        try:
            credential = get_auth_json_path().read_bytes()
        except OSError:
            return None  # Keyring identity cannot be established from this file.
    else:
        try:
            credential = json.dumps(provider_headers(provider), sort_keys=True).encode()
        except CredentialError:
            return None
    envelope = {
        "version": __version__,
        "endpoint": provider.base_url,
        "protocol": provider.wire_api,
        "authentication": provider.auth_type,
        "credentials": hashlib.sha256(credential).hexdigest(),
    }
    return hashlib.sha256(json.dumps(envelope, sort_keys=True).encode()).hexdigest()


def catalog_is_fresh(provider, ttl=3600):
    expected_source = (
        "codex:model/list" if provider.auth_type == "official_oauth" else "provider:/models"
    )
    if provider.catalog_source != expected_source:
        return False
    try:
        stamp = datetime.fromisoformat(provider.catalog_fetched_at)
        age = (datetime.now(timezone.utc) - stamp).total_seconds()
        scope = catalog_scope(provider)
        return bool(scope and scope == provider.catalog_scope and 0 <= age <= ttl)
    except (TypeError, ValueError):
        return False


def probe_remote_models(base_url, auth_token=None, wire_api="responses", *, provider=None):
    """Read every catalog page, keeping requests on the selected origin."""
    try:
        provider = provider or ProviderConfig(
            id="probe", name="Catalog", base_url=base_url, auth_token=auth_token, wire_api=wire_api
        )
        headers = provider_headers(provider)
        base = provider.base_url
        if provider.wire_api == "anthropic_proxy" and not urlparse(base).path.rstrip("/").endswith(
            "/v1"
        ):
            base += "/v1"
        endpoint = base + "/models"
        origin = (urlparse(endpoint).scheme, urlparse(endpoint).netloc)
        seen = set()
        collected = []
        with httpx.Client(timeout=10, follow_redirects=False) as client:
            while endpoint:
                if endpoint in seen:
                    return False, [], "Catalog pagination repeated a page."
                seen.add(endpoint)
                response = client.get(endpoint, headers=headers)
                if response.status_code != 200:
                    return (
                        False,
                        [],
                        f"Catalog request failed (HTTP {response.status_code}); inference was not tested.",
                    )
                body = response.json()
                if not isinstance(body, dict):
                    return False, [], "The endpoint returned an invalid model catalog."
                items = body.get("data", body.get("models"))
                if not isinstance(items, list):
                    return False, [], "The endpoint returned an invalid model catalog."
                collected.extend(items)
                next_url = (
                    (body.get("links") or {}).get("next")
                    if isinstance(body.get("links"), dict)
                    else None
                )
                if body.get("has_more"):
                    cursor = body.get("last_id")
                    if not isinstance(cursor, str) or not cursor:
                        return False, [], "The endpoint omitted its pagination cursor."
                    next_url = str(httpx.URL(base + "/models", params={"after_id": cursor}))
                if next_url:
                    endpoint = urljoin(endpoint, next_url)
                    parsed = urlparse(endpoint)
                    if (
                        (parsed.scheme, parsed.netloc) != origin
                        or parsed.path != urlparse(base + "/models").path
                        or parsed.username
                        or parsed.password
                        or parsed.fragment
                    ):
                        return False, [], "Catalog pagination points outside the selected endpoint."
                    if any(
                        key.lower() in {"key", "api_key", "token", "authorization"}
                        for key in httpx.URL(endpoint).params
                    ):
                        return False, [], "Catalog pagination contains credentials."
                else:
                    endpoint = None
        return True, _models(collected), None
    except CredentialError as exc:
        return False, [], str(exc)
    except (OSError, httpx.HTTPError, ValueError, TypeError):
        return False, [], "Catalog request failed; check credentials, endpoint and network."


def discover_models(provider):
    if provider.auth_type == "official_oauth":
        try:
            with CodexReader(overrides=['model_provider="openai"']) as reader:
                items = list(reader.pages("model/list", {"limit": 100, "includeHidden": False}))
            models = _models(items)
            return True, models, None
        except (CodexRpcError, OSError):
            return (
                False,
                [],
                "Codex model discovery failed. Check sign-in or enter an exact model ID.",
            )
    return probe_remote_models(provider.base_url, provider=provider)


def refresh_provider_models(provider):
    before = catalog_scope(provider)
    routing = (provider.base_url, provider.wire_api, provider.auth_type)
    ok, models, error = discover_models(provider)
    if ok:
        after = catalog_scope(provider)
        if before != after or routing != (provider.base_url, provider.wire_api, provider.auth_type):
            return (
                False,
                [],
                "Connection identity changed during discovery. Refresh the catalog again.",
            )
        manual = {m.id: m for m in provider.models if m.is_custom}
        remote_ids = {m.id for m in models}
        provider.models = [manual.get(m.id, m) for m in models] + [
            m for identifier, m in manual.items() if identifier not in remote_ids
        ]
        provider.catalog_source = (
            "codex:model/list" if provider.auth_type == "official_oauth" else "provider:/models"
        )
        provider.catalog_fetched_at = datetime.now(timezone.utc).isoformat()
        provider.catalog_scope = after
    return ok, models, error


def model_metadata(provider, model_id):
    """Keep saved labels while withholding stale remote capabilities and defaults."""
    model = next((item for item in provider.models if item.id == model_id), None)
    if model is None:
        return None
    if model.is_custom or catalog_is_fresh(provider):
        return model
    return ModelInfo(id=model.id, name=model.name)


def select_model(provider, model_id):
    model = model_metadata(provider, model_id)
    if model is None:
        model = ModelInfo(id=model_id, name=model_id, is_custom=True)
        provider.models.append(model)
    provider.default_model = model.id
    provider.model_selected = True
    if model.wire_api:
        provider.wire_api = model.wire_api
    if model.default_reasoning_effort:
        provider.reasoning_effort = model.default_reasoning_effort
    elif (
        model.supported_reasoning_levels
        and provider.reasoning_effort not in model.supported_reasoning_levels
    ):
        provider.reasoning_effort = model.supported_reasoning_levels[0]
    return model


def test_provider_connectivity(provider):
    if provider.auth_type == "official_oauth":
        return verify_openai_connectivity()
    start = time.perf_counter()
    ok, models, error = discover_models(provider)
    elapsed = int((time.perf_counter() - start) * 1000)
    return (
        ok,
        f"Catalog received ({len(models)} models); inference not tested."
        if ok
        else error or "Catalog unavailable.",
        elapsed,
    )
