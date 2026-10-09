"""Rendered headers fit real terminal widths without interpreting user markup."""

from io import StringIO
from unittest.mock import patch

import pytest
from rich.console import Console

from codex_mgr.utils.display import CUSTOM_THEME, print_banner


@pytest.mark.parametrize("width", [24, 40, 60, 80, 120])
def test_header_fits_small_and_large_terminals(width, monkeypatch):
    monkeypatch.setenv("CMGR_ASCII_MODE", "1")
    stream = StringIO()
    console = Console(file=stream, width=width, color_system=None, theme=CUSTOM_THEME)
    with patch("codex_mgr.utils.display.console", console):
        print_banner(
            active_provider="A very long connection name [red]literal[/red]",
            active_model="organization/a-very-long-model-identifier",
            reasoning_effort="high",
            context_cap=123456,
        )
    rendered = stream.getvalue()
    assert all(len(line) <= width for line in rendered.splitlines())
    assert all(ord(character) < 128 for character in rendered)
    if width >= 40:
        assert "Provider" in rendered and "Model" in rendered and "Context" in rendered
