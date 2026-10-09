# Codex Manager

Manage Codex CLI providers, models, and saved sessions from your terminal.

`cmgr` brings provider settings, model selection, session resumption, and recovery tools into one place. It runs alongside [Codex CLI](https://developers.openai.com/codex/cli/) and uses your existing Codex home directory.

## Project status

The current release needs compatibility and recovery fixes before it can be recommended for everyday use. In particular, provider authentication, cross-provider resumption, and the Anthropic adapter have known issues. See [compatibility](docs/wiki/Compatibility.md) for the current limits.

## Install from source

Requirements: Python 3.10 or later and an installed Codex CLI.

```sh
git clone https://github.com/MayukXT/codex-mgr.git
cd codex-mgr
python -m pip install .
cmgr
```

For development, install with `python -m pip install -e ".[dev]"`.

## What it provides

- A terminal menu for starting and resuming Codex sessions.
- Saved provider configurations and model choices.
- A provider setup wizard with model listing and manual model entry.
- Configuration export and import.
- Diagnostic checks and local backup tools.
- A session bridge that changes provider metadata and removes selected encrypted reasoning fields.

The bridge changes saved session files. Removing encrypted reasoning also removes information that cannot be reconstructed from the remaining conversation. Backups are intended to preserve the original file, but the current backup lookup needs repair.

## Commands

```sh
cmgr --help
cmgr                         # Open the terminal menu
cmgr provider add            # Configure a provider
cmgr switch                  # Show provider IDs and the saved active provider
cmgr doctor                  # Check local configuration and endpoint reachability
cmgr export --output setup.json --sanitize-keys
cmgr import setup.json
cmgr --version
cmgr update --check
```

The CLI also includes `resume`, `restore`, and `switch <provider-id>`. Their current implementation has known issues; review the compatibility notes before using them with existing sessions or configuration.

`codex-mgr` is an alias for `cmgr`.

## Providers and authentication

Codex configuration separates the selected model from the connection used to run it:

- `model` is the model ID sent to the endpoint.
- `model_provider` selects a provider definition.
- `[model_providers.<id>]` contains that connection's URL, authentication, and transport settings.

For a custom connection, the ID must match `model_provider`. A friendly provider name does not determine the model or grant access to it. Current Codex supports the Responses protocol for custom providers; a Chat Completions endpoint needs a compatible adapter. See [Codex configuration](https://developers.openai.com/codex/config-advanced/).

Signing in with ChatGPT and using an OpenAI API key are separate authentication methods. The current manager groups both under one OpenAI preset; explicit choices and correct credential handoff are planned. The Anthropic preset exists, but its adapter is not ready for Codex Responses traffic.

Provider presets are configuration starting points. A preset or a successful model-list request does not establish that a model supports the complete coding workflow. Model IDs and capabilities should be checked against the selected provider's current catalog.

## Files and privacy

By default, the manager uses `~/.codex/`; it respects `CODEX_HOME` when configured.

| File or directory | Purpose |
| --- | --- |
| `cmgr.json` | Manager settings, provider definitions, and any pasted keys |
| `config.toml` | Codex configuration updated by provider switching |
| `sessions/` | Codex session rollouts |
| `backups/` | Copies of configuration, settings, and modified sessions |

Pasted keys are stored as plaintext in local settings and may also appear in config backups. Export includes keys unless `--sanitize-keys` is supplied. Sanitized exports remove the dedicated token field; endpoints and other user-entered fields still need review before sharing.

The manager makes network requests for provider checks, model lists, and update checks. Codex sends prompts and code to the selected inference endpoint when you run a session.

## Development

Source code is in `src/codex_mgr/`; tests are in `tests/`.

```sh
python -m pytest tests -q
```

The unit tests use synthetic fixtures. They do not establish live provider compatibility or complete workflow coverage.

See the [usage guide](docs/wiki/Home.md) and [contributor guide](FORDEV.md).

## License

[MIT](LICENSE).
