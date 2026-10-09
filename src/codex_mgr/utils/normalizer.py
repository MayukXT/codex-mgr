"""Sanitization and normalization helpers for URLs and API credentials.

Prevents common user errors like trailing slashes, duplicate /v1 paths,
and copy-pasted whitespace or quotes. Masks secrets safely in all visual output.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse, urlunparse


def normalize_base_url(url: str, wire_api: str = "responses") -> str:
    """Normalize and validate a model provider Base URL.

    - Strips surrounding quotes and whitespace.
    - Ensures standard scheme (http:// or https://).
    - Removes trailing slashes.
    - Fixes duplicated /v1/v1 suffixes.
    - Appends /v1 if missing for standard REST endpoints when appropriate.
    """
    cleaned = url.strip().strip("'\"").strip()
    if not cleaned:
        return ""

    if not cleaned.startswith(("http://", "https://")):
        # Default to https unless localhost
        if cleaned.startswith("localhost") or cleaned.startswith("127.0.0.1"):
            cleaned = f"http://{cleaned}"
        else:
            cleaned = f"https://{cleaned}"

    parsed = urlparse(cleaned)
    path = parsed.path.rstrip("/")

    # Fix duplicate /v1/v1
    while path.endswith("/v1/v1"):
        path = path[:-3]

    # For standard responses/chat endpoints that omit /v1 entirely
    if not path and not parsed.netloc.endswith(":11434"):
        # e.g. api.deepseek.com -> api.deepseek.com/v1
        path = "/v1"

    normalized = urlunparse((
        parsed.scheme,
        parsed.netloc,
        path,
        parsed.params,
        parsed.query,
        parsed.fragment
    ))
    return normalized.rstrip("/")


def sanitize_api_key(raw_key: str) -> str:
    """Clean an API key or token string.

    Strips whitespace, surrounding single/double quotes, and control characters.
    """
    if not raw_key:
        return ""
    # Strip whitespace, quotes, and newlines
    cleaned = raw_key.strip().strip("'\"").strip()
    # Remove any internal carriage returns
    return re.sub(r"[\r\n\t]", "", cleaned)


def mask_api_key(key: str) -> str:
    """Mask an API key for safe public UI rendering.

    Never exposes full tokens in terminal output or logs.
    Example: 'sk-43LDpfHe93CCWv4u5dhjeLFU' -> 'sk-...eLFU'
    """
    if not key:
        return "[none]"
    if key.startswith("$"):
        return key  # Environment variable reference is safe to show

    clean = sanitize_api_key(key)
    if len(clean) <= 8:
        return "********"

    prefix = clean[:3] if clean.startswith("sk-") else clean[:2]
    suffix = clean[-4:]
    return f"{prefix}...{suffix}"


def is_env_var_reference(text: str) -> bool:
    """Check if the given token input represents an environment variable reference."""
    clean = text.strip()
    if clean.startswith("$"):
        var_name = clean[1:]
        return bool(re.match(r"^[A-Z0-9_]+$", var_name))
    return bool(re.match(r"^[A-Z][A-Z0-9_]{3,}$", clean) and not clean.startswith("sk-"))
