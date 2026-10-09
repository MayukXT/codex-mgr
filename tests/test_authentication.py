"""Auth status must not infer access from cached tokens or replay OAuth keys."""

import json
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.core.auth import (
    CredentialError,
    LoginStatus,
    codex_login_status,
    get_effective_openai_api_key,
    get_openai_oauth_info,
    resolve_credential,
    verify_openai_connectivity,
)


def test_provider_repr_hides_credentials_and_headers():
    provider = ProviderConfig(
        id="example",
        name="Example",
        base_url="https://api.example/v1",
        auth_token="synthetic-secret",
        http_headers={"Authorization": "synthetic-header"},
        env_http_headers={"X-Key": "PRIVATE_ENV_REFERENCE"},
    )
    assert "synthetic-secret" not in repr(provider)
    assert "synthetic-header" not in repr(provider)
    assert "PRIVATE_ENV_REFERENCE" not in repr(provider)


def test_selected_environment_variable_never_falls_back(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-other-key")
    monkeypatch.setenv("SELECTED_KEY", "synthetic-selected-key")
    assert get_effective_openai_api_key("$SELECTED_KEY") == "synthetic-selected-key"
    with pytest.raises(CredentialError):
        get_effective_openai_api_key("$MISSING_SELECTED_KEY")


def test_unknown_plan_and_expiry_remain_unknown(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "auth.json").write_text(
        json.dumps(
            {"tokens": {"access_token": "synthetic-token", "refresh_token": "synthetic-refresh"}}
        )
    )
    info = get_openai_oauth_info()
    assert info.plan_type == "unknown"
    assert info.is_expired is None
    assert "synthetic-token" not in repr(info)
    assert "synthetic-refresh" not in repr(info)


def test_oauth_health_uses_codex_status_instead_of_http_token_probe():
    with patch(
        "codex_mgr.core.auth.codex_login_status",
        return_value=LoginStatus(False, "unknown", "Sign-in pending"),
    ):
        assert not verify_openai_connectivity()[0]
    with patch(
        "codex_mgr.core.auth.codex_login_status",
        return_value=LoginStatus(True, "api_key", "API-key login"),
    ):
        assert not verify_openai_connectivity()[0]


def test_login_status_does_not_echo_key_details():
    response = CompletedProcess(
        [], 0, "", "Logged in using an API key - synthetic-private-detail\nChatGPT recommendation"
    )
    with (
        patch("codex_mgr.core.auth.find_codex_binary", return_value=Path("synthetic-codex")),
        patch("codex_mgr.core.auth.subprocess.run", return_value=response),
    ):
        status = codex_login_status()
    assert status.signed_in and status.method == "api_key"
    assert "synthetic-private-detail" not in status.message


@pytest.mark.parametrize("token", ["$INVALID NAME", "synthetic\nkey", "synthetic\x00key"])
def test_invalid_credential_input_is_rejected_without_echo(token):
    with pytest.raises(CredentialError) as error:
        resolve_credential(token)
    assert token not in str(error.value)


@pytest.mark.parametrize("status", [401, 403])
def test_authentication_error_never_counts_as_success(status):
    import httpx

    response = httpx.Response(status, request=httpx.Request("GET", "https://api.example/models"))
    with patch("codex_mgr.core.auth.httpx.Client") as factory:
        factory.return_value.__enter__.return_value.get.return_value = response
        assert not verify_openai_connectivity(api_key="synthetic-key")[0]
