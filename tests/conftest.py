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


@pytest.fixture(autouse=True)
def synthetic_native_configuration(request, monkeypatch):
    """Default tests model native user-layer reads; native contracts use the binary."""
    if request.node.path.name == "test_native_codex.py":
        return
    from codex_mgr.core import native_config
    from codex_mgr.core.profiles import parse_configuration
    from codex_mgr.utils.files import read_optional
    from codex_mgr.utils.paths import get_config_toml_path

    class Reader:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def request(self, method, params=None):
            if method == "configRequirements/read":
                return {"requirements": None}
            assert method == "config/read"
            assert params["includeLayers"] is True
            path = get_config_toml_path()
            config = parse_configuration(read_optional(path)).unwrap()
            origin = {"name": {"type": "user", "file": str(path), "profile": None}}
            return {"config": config, "origins": {key: origin for key in config}}

    monkeypatch.setattr(native_config, "CodexReader", Reader)
