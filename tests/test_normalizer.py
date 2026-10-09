"""Unit tests for URL and API key normalization."""

from codex_mgr.utils.normalizer import (
    is_env_var_reference,
    mask_api_key,
    normalize_base_url,
    sanitize_api_key,
)


def test_normalize_base_url_trailing_slash():
    url = "https://api.openai.com/v1/"
    assert normalize_base_url(url) == "https://api.openai.com/v1"


def test_normalize_base_url_duplicate_v1():
    url = "https://openrouter.ai/api/v1/v1"
    assert normalize_base_url(url) == "https://openrouter.ai/api/v1"


def test_normalize_base_url_missing_scheme():
    url = "agentrouter.org/v1"
    assert normalize_base_url(url) == "https://agentrouter.org/v1"


def test_normalize_base_url_localhost():
    url = "localhost:11434"
    assert normalize_base_url(url) == "http://localhost:11434"


def test_sanitize_api_key_whitespace_and_quotes():
    raw = "  'sk-test-token-12345' \n"
    assert sanitize_api_key(raw) == "sk-test-token-12345"


def test_mask_api_key():
    key = "synthetic-private-credential"
    masked = mask_api_key(key)
    assert masked == "[set]"
    assert "private" not in masked


def test_is_env_var_reference():
    assert is_env_var_reference("$AGENTROUTER_KEY") is True
    assert is_env_var_reference("OPENROUTER_API_KEY") is True
    assert is_env_var_reference("sk-43LDpfHe") is False
