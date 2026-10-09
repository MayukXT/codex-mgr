"""Dynamic model discovery, local cache synchronization, and zero-token health probing.

Consumes 0 prompt tokens by querying standard metadata endpoints (/v1/models)
and syncs natively with Codex's local models_cache.json.
"""

from __future__ import annotations

import json
import time
from typing import List, Optional, Tuple

import httpx

from codex_mgr.config.schema import ModelInfo, ProviderConfig
from codex_mgr.utils.normalizer import normalize_base_url
from codex_mgr.utils.paths import get_models_cache_path


def sync_models_from_local_cache() -> List[ModelInfo]:
    """Extract models from the local ~/.codex/models_cache.json file if available."""
    cache_path = get_models_cache_path()
    if not cache_path.exists():
        return []

    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        raw_models = data.get("models", [])
        discovered: List[ModelInfo] = []

        for m in raw_models:
            slug = m.get("slug")
            name = m.get("display_name", slug)
            levels = [lvl.get("effort") for lvl in m.get("supported_reasoning_levels", []) if "effort" in lvl]
            if not levels:
                levels = ["low", "medium", "high", "xhigh", "max"]

            discovered.append(ModelInfo(
                id=slug,
                name=name,
                context_window=512000 if "6" in slug else 256000,
                supported_reasoning_levels=levels,
                is_custom=False
            ))
        return discovered
    except Exception:
        return []


def probe_remote_models(
    base_url: str,
    auth_token: Optional[str] = None,
    wire_api: str = "responses"
) -> Tuple[bool, List[ModelInfo], Optional[str]]:
    """Probe a remote provider endpoint for available models using zero prompt tokens.

    Returns:
        (success, list_of_models, error_message)
    """
    clean_url = normalize_base_url(base_url, wire_api)
    models_endpoint = f"{clean_url}/models"

    headers = {"User-Agent": "codex-mgr/1.0.0"}
    if auth_token and not auth_token.startswith("$"):
        headers["Authorization"] = f"Bearer {auth_token}"

    try:
        with httpx.Client(timeout=6.0, follow_redirects=True) as client:
            resp = client.get(models_endpoint, headers=headers)

            if resp.status_code == 200:
                body = resp.json()
                raw_list = body.get("data", []) or body.get("models", [])
                models: List[ModelInfo] = []

                for item in raw_list:
                    mid = item.get("id") or item.get("name")
                    if mid:
                        models.append(ModelInfo(
                            id=mid,
                            name=item.get("display_name") or mid,
                            context_window=item.get("context_window", 256000),
                            is_custom=False
                        ))
                return True, models, None

            if resp.status_code == 401:
                return False, [], "Invalid API Key or Bearer Token (HTTP 401 Unauthorized)."
            if resp.status_code == 403:
                return False, [], "Forbidden: Access denied to this endpoint (HTTP 403 Forbidden)."
            if resp.status_code == 404:
                return False, [], "The /models endpoint is not supported by this server (HTTP 404)."
            if resp.status_code == 429:
                return False, [], "Rate limit reached or quota depleted (HTTP 429)."

            return False, [], f"Server returned unexpected HTTP status: {resp.status_code}"

    except httpx.ConnectError:
        return False, [], f"Could not connect to {clean_url}. Is the service running?"
    except httpx.TimeoutException:
        return False, [], "Connection timed out after 6 seconds."
    except Exception as exc:
        return False, [], f"Network error: {str(exc)}"


def test_provider_connectivity(provider: ProviderConfig) -> Tuple[bool, str, int]:
    """Perform a zero-token reachability and latency health test for a provider.

    Returns:
        (is_healthy, status_message, latency_ms)
    """
    start_time = time.perf_counter()

    # If Anthropic direct, test official endpoint or local proxy
    if provider.wire_api == "anthropic_proxy":
        target_url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": provider.auth_token or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        try:
            with httpx.Client(timeout=5.0) as client:
                # Minimal probe: empty payload to test authentication handshake
                resp = client.post(target_url, headers=headers, json={"max_tokens": 1, "messages": []})
                latency_ms = int((time.perf_counter() - start_time) * 1000)

                # HTTP 400 with invalid messages array confirms API key is authentic!
                if resp.status_code in (200, 400):
                    return True, "Anthropic API connection verified (Handshake OK)", latency_ms
                if resp.status_code == 401:
                    return False, "Invalid Anthropic API Key (HTTP 401)", latency_ms
                return False, f"Unexpected response: HTTP {resp.status_code}", latency_ms
        except Exception as e:
            latency_ms = int((time.perf_counter() - start_time) * 1000)
            return False, f"Connection failed: {str(e)}", latency_ms

    # Standard OpenAI / Router endpoints
    ok, models, err = probe_remote_models(provider.base_url, provider.auth_token, provider.wire_api)
    latency_ms = int((time.perf_counter() - start_time) * 1000)

    if ok:
        count = len(models)
        return True, f"Connection verified ({count} models available)", latency_ms

    # If /models 404, try root ping
    if "404" in str(err):
        try:
            with httpx.Client(timeout=4.0) as client:
                resp = client.get(provider.base_url)
                latency_ms = int((time.perf_counter() - start_time) * 1000)
                if resp.status_code < 500:
                    return True, "Endpoint reachable (Base URL responding)", latency_ms
        except Exception:
            pass

    return False, err or "Connection failed", latency_ms
