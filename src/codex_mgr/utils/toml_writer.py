"""TOML serialization using the required style-preserving parser."""

from __future__ import annotations

from typing import Any

import tomlkit


def dump_toml(data: dict[str, Any]) -> str:
    return tomlkit.dumps(data)
