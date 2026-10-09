"""Credential resolution and authentication delegated to Codex CLI."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass, field

import httpx

from codex_mgr.utils.paths import find_codex_binary, get_auth_json_path


class CredentialError(ValueError):
    pass


@dataclass(frozen=True)
class LoginStatus:
    signed_in: bool
    method: str
    message: str


def codex_login_status() -> LoginStatus:
    binary = find_codex_binary()
    if binary is None:
        return LoginStatus(False, "unavailable", "Codex CLI is not installed.")
    try:
        result = subprocess.run(
            [str(binary), "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        output = (result.stdout + result.stderr).lower()
        auth_line = next(
            (
                line.strip()
                for line in output.splitlines()
                if line.strip().startswith("logged in using")
            ),
            "",
        )
        if result.returncode == 0 and "chatgpt" in auth_line:
            return LoginStatus(
                True, "chatgpt", "Signed in with ChatGPT; inference access was not tested."
            )
        if result.returncode == 0 and "api key" in auth_line:
            return LoginStatus(True, "api_key", "Codex is signed in with an API key.")
        return LoginStatus(False, "unknown", "ChatGPT sign-in is not confirmed. Run codex login.")
    except (OSError, subprocess.TimeoutExpired):
        return LoginStatus(False, "unavailable", "Could not read Codex login status.")


def login_with_codex(*, device_auth: bool = False) -> tuple[bool, str]:
    binary = find_codex_binary()
    if binary is None:
        return False, "Install Codex CLI before signing in."
    command = [str(binary), "login"]
    if device_auth:
        command.append("--device-auth")
    try:
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            return False, "Codex sign-in did not complete."
        status = codex_login_status()
        return status.signed_in and status.method == "chatgpt", status.message
    except (OSError, KeyboardInterrupt):
        return False, "Codex sign-in was cancelled or could not start."


def resolve_credential(configured_token: str | None) -> str | None:
    if not configured_token:
        return None
    if configured_token.startswith("$"):
        name = configured_token[1:]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise CredentialError("Invalid credential environment-variable name.")
        value = os.environ.get(name)
        if not value or not value.strip():
            raise CredentialError(f"Credential environment variable {name} is not set.")
        configured_token = value.strip()
    if any(ord(c) < 32 for c in configured_token):
        raise CredentialError("Credentials cannot contain control characters.")
    return configured_token


@dataclass
class OpenAIOAuthInfo:
    """Unverified file metadata, never proof of account access or subscription."""

    plan_type: str
    plan_display: str
    email: str | None = field(repr=False)
    account_id: str | None = field(repr=False)
    expires_at: int | None
    is_expired: bool | None
    has_refresh_token: bool
    access_token: str | None = field(repr=False)


def parse_jwt_payload(token: str) -> dict:
    """Decode unverified metadata; this does not validate a JWT signature."""
    try:
        encoded = token.split(".")[1]
        decoded = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        payload = json.loads(decoded)
        return payload if isinstance(payload, dict) else {}
    except (IndexError, ValueError, binascii.Error):
        return {}


def get_openai_oauth_info() -> OpenAIOAuthInfo | None:
    try:
        raw = json.loads(get_auth_json_path().read_text(encoding="utf-8"))
        tokens = raw.get("tokens", {})
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            return None
        payload = parse_jwt_payload(token)
        auth = payload.get("https://api.openai.com/auth", {})
        auth = auth if isinstance(auth, dict) else {}
        plan = auth.get("chatgpt_plan_type")
        plan = plan if isinstance(plan, str) and plan else "unknown"
        expiry = payload.get("exp")
        expiry = expiry if isinstance(expiry, int) and not isinstance(expiry, bool) else None
        return OpenAIOAuthInfo(
            plan,
            "ChatGPT (plan unverified)",
            None,
            None,
            expiry,
            time.time() > expiry if expiry is not None else None,
            bool(tokens.get("refresh_token")),
            token,
        )
    except (OSError, ValueError, AttributeError):
        return None


def get_effective_openai_api_key(configured_token: str | None = None) -> str | None:
    if configured_token is not None:
        return resolve_credential(configured_token)
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return resolve_credential(key)
    try:
        data = json.loads(get_auth_json_path().read_text(encoding="utf-8"))
        key = data.get("OPENAI_API_KEY")
        return resolve_credential(key) if isinstance(key, str) else None
    except (OSError, ValueError, AttributeError):
        return None


def verify_openai_connectivity(
    api_key: str | None = None,
    oauth_info: OpenAIOAuthInfo | None = None,
    *,
    base_url: str = "https://api.openai.com/v1",
) -> tuple[bool, str, int]:
    if api_key is None:
        status = codex_login_status()
        return status.signed_in and status.method == "chatgpt", status.message, 0
    start = time.perf_counter()
    try:
        key = resolve_credential(api_key)
        if not key:
            return False, "An API key is required.", 0
        with httpx.Client(timeout=6, follow_redirects=False) as client:
            response = client.get(
                base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {key}"}
            )
        elapsed = int((time.perf_counter() - start) * 1000)
        if response.status_code == 200 and isinstance(response.json().get("data"), list):
            return (
                True,
                "Authenticated model list received; inference access was not tested.",
                elapsed,
            )
        return False, f"Model list request failed (HTTP {response.status_code}).", elapsed
    except (httpx.HTTPError, ValueError, AttributeError):
        return (
            False,
            "Could not authenticate or read the model list.",
            int((time.perf_counter() - start) * 1000),
        )
