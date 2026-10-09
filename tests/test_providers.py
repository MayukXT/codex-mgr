"""Provider identity, template separation and reversible management."""

import pytest

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.config.settings import load_settings
from codex_mgr.core.profiles import prepare_configuration
from codex_mgr.core.providers import delete_provider, get_default_presets, store_provider


def test_auth_templates_are_distinct_and_do_not_invent_models():
    presets = get_default_presets()
    assert presets["openai"].auth_type == "official_oauth"
    assert presets["openai_api"].auth_token == "$OPENAI_API_KEY"
    assert presets["anthropic"].auth_token == "$ANTHROPIC_API_KEY"
    assert presets["opencode"].base_url == "https://opencode.ai/zen/v1"
    for item in presets.values():
        assert not item.models and not item.model_selected
        with pytest.raises(ValueError, match="model ID"):
            prepare_configuration(item, None)


def test_id_collision_cannot_overwrite_saved_connection():
    settings = load_settings()
    item = ProviderConfig(
        id="example",
        name="Example",
        base_url="https://api.example/v1",
        default_model="exact",
        auth_type="none",
    )
    assert store_provider(settings, item)[0]
    changed = item.model_copy(update={"name": "Changed"})
    assert not store_provider(load_settings(), changed)[0]
    assert load_settings().providers["example"].name == "Example"
    assert store_provider(load_settings(), changed, replace=True)[0]
    assert load_settings().providers["example"].name == "Changed"


def test_deletion_does_not_resurrect_template_or_delete_active():
    settings = load_settings()
    assert not delete_provider(settings, settings.active_provider_id)[0]
    template = get_default_presets()["anthropic"]
    template.default_model = "exact"
    template.model_selected = True
    assert store_provider(settings, template)[0]
    assert delete_provider(load_settings(), "anthropic")[0]
    assert "anthropic" not in load_settings().providers


@pytest.mark.parametrize(
    "values",
    [
        {"id": "../unsafe"},
        {"base_url": "https://api.example/v1?key=synthetic"},
        {"base_url": "https://user:synthetic@api.example/v1"},
        {"proxy_port": 0},
        {"http_headers": {"X-Key": "synthetic\nheader"}},
    ],
)
def test_sensitive_or_invalid_connection_input_is_rejected(values):
    data = {"id": "example", "name": "Example", "base_url": "https://api.example/v1"}
    data.update(values)
    with pytest.raises(ValueError):
        ProviderConfig(**data)
