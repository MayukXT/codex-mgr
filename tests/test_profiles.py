"""Unit tests for surgical config.toml merging and preservation."""

from pathlib import Path

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.core.profiles import read_config_toml, switch_active_provider
from codex_mgr.utils.paths import get_config_toml_path


def test_switch_preserves_mcp_servers_and_hooks(tmp_path: Path):
    mock_config = get_config_toml_path()
    mock_config.parent.mkdir(parents=True)
    initial_toml = """
model = "gpt-6-astra"
model_reasoning_effort = "max"

[marketplaces.openai-bundled]
source_type = "local"
source = "mock/path"

[mcp_servers.kaggle]
url = "https://www.kaggle.com/mcp"
bearer_token_env_var = "KGAT_mock_token"

[hooks.state]
trusted_hash = "sha256:mock_hash_123"

[desktop]
appearanceTheme = "dark"
sansFontSize = 16
"""
    mock_config.write_text(initial_toml.strip(), encoding="utf-8")

    provider = ProviderConfig(
        id="agentrouter",
        name="AgentRouter",
        base_url="https://agentrouter.org/v1",
        wire_api="responses",
        auth_type="bearer_token",
        auth_token="sk-mock-router-key",
        default_model="gpt-5.6-sol",
        reasoning_effort="max",
    )

    ok, msg = switch_active_provider(provider)

    assert ok is True, msg

    # Read back and verify
    updated = read_config_toml()

    # Provider and model updated
    assert updated["model"] == "gpt-5.6-sol"
    assert updated["model_provider"] == "agentrouter"
    assert updated["model_providers"]["agentrouter"]["name"] == "AgentRouter"

    # MCP servers strictly preserved
    assert "kaggle" in updated["mcp_servers"]
    assert updated["mcp_servers"]["kaggle"]["url"] == "https://www.kaggle.com/mcp"

    # Hooks and desktop preserved
    assert updated["hooks"]["state"]["trusted_hash"] == "sha256:mock_hash_123"
    assert updated["desktop"]["appearanceTheme"] == "dark"
