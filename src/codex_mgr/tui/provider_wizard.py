"""Concise provider setup and editing with distinct credential paths."""

from __future__ import annotations

import re

from InquirerPy import inquirer
from InquirerPy.base.control import Choice

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.config.settings import load_settings
from codex_mgr.core.auth import codex_login_status, login_with_codex, resolve_credential
from codex_mgr.core.models import refresh_provider_models, select_model
from codex_mgr.core.providers import get_default_presets, store_provider
from codex_mgr.utils.display import console, print_error, print_success, print_warning, select_menu


def _text(message, default=""):
    return inquirer.text(message=message, default=default).execute().strip()


def choose_model(provider):
    """Search the complete catalog or enter an exact ID."""
    choices = [
        Choice(value=m.id, name=f"{m.name} ({m.id})" if m.name != m.id else m.id)
        for m in provider.models
    ]
    choices += [Choice(value="__manual__", name="Enter model ID"), Choice(value=None, name="Back")]
    chosen = inquirer.fuzzy(
        message="Model:", choices=choices, max_height="60%", mandatory=False
    ).execute()
    if not chosen:
        return False
    if chosen == "__manual__":
        chosen = _text("Exact model ID:", provider.default_model if provider.model_selected else "")
        if not chosen:
            return False
    model = select_model(provider, chosen)
    if provider.id.startswith("opencode") and model.wire_api is None:
        # Zen uses different protocols per model. Require an explicit choice.
        provider.wire_api = (
            select_menu(
                "Protocol documented for this model:",
                [
                    Choice("responses", "Responses"),
                    Choice("anthropic_proxy", "Anthropic Messages"),
                    Choice("chat", "Chat Completions"),
                ],
                allow_back=False,
            )
            or "responses"
        )
    return True


def run_provider_wizard(existing_id=None):
    settings = load_settings()
    existing = settings.providers.get(existing_id) if existing_id else None
    if existing_id and existing is None:
        print_error("Connection not found.")
        return None
    console.print("\n[bold]Edit connection[/]" if existing else "\n[bold]Add connection[/]")
    if existing:
        provider = existing.model_copy(deep=True)
    else:
        presets = get_default_presets()
        selected = select_menu(
            "Connection:",
            [Choice(pid, p.name) for pid, p in presets.items()]
            + [Choice("custom", "Custom endpoint"), Choice(None, "Back")],
        )
        if not selected:
            return None
        provider = (
            presets[selected].model_copy(deep=True)
            if selected in presets
            else ProviderConfig(
                id="custom",
                name="Custom endpoint",
                base_url="https://api.example/v1",
                model_selected=False,
            )
        )
        suggested = provider.id
        suffix = 2
        while suggested in settings.providers and settings.providers[suggested].model_selected:
            suggested = f"{provider.id}_{suffix}"
            suffix += 1
        provider.id = _text("Connection ID:", suggested)
        provider.is_preset = False
    provider.name = _text("Name:", provider.name)
    if provider.auth_type != "official_oauth":
        provider.base_url = _text("Endpoint:", provider.base_url)
        provider.wire_api = select_menu(
            "Protocol:",
            [
                Choice("responses", "Responses"),
                Choice("chat", "Chat Completions"),
                Choice("anthropic_proxy", "Anthropic Messages"),
            ],
            default=provider.wire_api,
            allow_back=False,
        )
        mode = select_menu(
            "Credentials:",
            [
                Choice("env", "Environment variable"),
                Choice("paste", "Paste key (plaintext storage)"),
                Choice("none", "No authentication"),
                Choice("keep", "Keep current") if existing else Choice(None, "Back"),
            ],
            default="env" if provider.auth_type != "none" else "none",
        )
        if not mode:
            return None
        if mode == "env":
            reference = _text(
                "Variable name:",
                provider.auth_token
                if provider.auth_token and provider.auth_token.startswith("$")
                else f"${provider.id.upper()}_API_KEY",
            )
            if not re.fullmatch(r"\$?[A-Za-z_][A-Za-z0-9_]*", reference):
                print_error("Enter a valid environment variable name.")
                return None
            provider.auth_token = "$" + reference.lstrip("$")
            provider.auth_type = "env_var"
        elif mode == "paste":
            provider.auth_token = inquirer.secret(message="API key:").execute().strip()
            if not provider.auth_token:
                print_error("A key is required.")
                return None
            resolve_credential(provider.auth_token)
            provider.auth_type = "bearer_token"
        elif mode == "none":
            provider.auth_type, provider.auth_token = "none", None
    else:
        status = codex_login_status()
        if not (status.signed_in and status.method == "chatgpt"):
            if inquirer.confirm(message="Sign in with Codex now?", default=True).execute():
                ok, message = login_with_codex()
                if not ok:
                    print_error(message)
                    return None
            else:
                print_warning("Run codex login before launching.")
    try:
        provider = ProviderConfig.model_validate(provider.model_dump())
    except ValueError:
        print_error("Invalid connection. Check ID, endpoint, port and credentials.")
        return None
    if inquirer.confirm(message="Load model catalog?", default=True).execute():
        ok, _, error = refresh_provider_models(provider)
        if not ok:
            print_warning(error or "Catalog unavailable; enter a model ID.")
    if not choose_model(provider):
        return None
    activate = inquirer.confirm(message="Use this connection now?", default=True).execute()
    ok, message = store_provider(
        settings, provider, replace=existing is not None, activate=activate
    )
    (print_success if ok else print_error)(message)
    return provider if ok else None
