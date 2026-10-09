"""Lightweight standalone TOML serializer with automatic tomli_w fallback.

Guarantees that codex-mgr can write config.toml accurately even in zero-dependency
or minimal runtime environments.
"""

from __future__ import annotations

import json
from typing import Any, Dict


def dump_toml(data: Dict[str, Any]) -> str:
    """Serialize a dictionary to valid TOML string.

    Uses tomli_w if available; otherwise falls back to a clean built-in serializer.
    """
    try:
        import tomli_w
        return tomli_w.dumps(data)
    except ImportError:
        return _serialize_toml(data)


def _format_value(val: Any) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, str):
        return json.dumps(val)
    if isinstance(val, list):
        items = [_format_value(x) for x in val]
        return f"[{', '.join(items)}]"
    if isinstance(val, dict):
        items = [f"{k} = {_format_value(v)}" for k, v in val.items()]
        return f"{{{', '.join(items)}}}"
    return '""'


def _serialize_toml(data: Dict[str, Any]) -> str:
    lines = []
    tables = {}

    # Top-level key-values first
    for k, v in data.items():
        if isinstance(v, dict):
            tables[k] = v
        else:
            lines.append(f"{k} = {_format_value(v)}")

    if lines and tables:
        lines.append("")

    # Tables
    def _emit_table(prefix: str, tbl: Dict[str, Any]):
        sub_tables = {}
        for tk, tv in tbl.items():
            if isinstance(tv, dict):
                sub_tables[f"{prefix}.{tk}"] = tv
            else:
                lines.append(f"{tk} = {_format_value(tv)}")

        for sub_name, sub_tbl in sub_tables.items():
            lines.append("")
            lines.append(f"[{sub_name}]")
            _emit_table(sub_name, sub_tbl)

    for table_name, table_dict in tables.items():
        lines.append(f"[{table_name}]")
        _emit_table(table_name, table_dict)
        lines.append("")

    return "\n".join(lines).strip() + "\n"
