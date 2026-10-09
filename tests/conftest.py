"""Keep every test away from real configuration, credentials, and HTTP traffic."""

import httpx
import pytest


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    monkeypatch.setenv("CODEX_SQLITE_HOME", str(tmp_path / "codex-sqlite"))
    for variable in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(variable, raising=False)

    def deny_network(*args, **kwargs):
        raise AssertionError("Tests must use an explicit mock HTTP transport.")

    async def deny_async_network(*args, **kwargs):
        raise AssertionError("Tests must use an explicit mock HTTP transport.")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async_network)
