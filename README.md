# Codex Manager

Manage Codex CLI providers, models, and saved sessions from your terminal.

`cmgr` brings provider settings, model selection, session resumption, and recovery tools into one place. It runs alongside [Codex CLI](https://developers.openai.com/codex/cli/) and uses your existing Codex home directory.

## Project status

Compatibility and recovery work is underway for v1.1.0. Cross-provider resumption and protocol adapters still need complete workflow validation before this version can be recommended for everyday use. See [compatibility](docs/wiki/Compatibility.md) for the current limits.

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
cmgr provider edit my-api    # Edit a saved connection
cmgr provider test my-api    # Check catalog access; no inference request
cmgr model list --refresh    # Load the active connection's catalog
cmgr model use MODEL_ID      # Select an exact model ID
cmgr switch                  # Show provider IDs and the saved active provider
cmgr doctor                  # Check local configuration and endpoint reachability
cmgr recover                 # Recover an interrupted configuration change
cmgr export --output setup.json --sanitize-keys
cmgr import setup.json
cmgr --version
cmgr update --check
```

The CLI also includes `resume`, `restore`, and `switch <provider-id>`. Their current implementation has known issues; review the compatibility notes before using them with existing sessions or configuration.

Provider changes preserve TOML comments and unrelated settings, honor the selected Codex profile, and keep recovery snapshots. Invalid files or conflicting external edits stop a change. Run `cmgr recover` after an interrupted change; conflicts require review before recovery can proceed.

`codex-mgr` is an alias for `cmgr`.

## Providers and authentication

Codex configuration separates the selected model from the connection used to run it:

- `model` is the model ID sent to the endpoint.
- `model_provider` selects a provider definition.
- `[model_providers.<id>]` contains that connection's URL, authentication, and transport settings.

For a custom connection, the ID must match `model_provider`. A friendly provider name does not determine the model or grant access to it. Current Codex supports the Responses protocol for custom providers; a Chat Completions endpoint needs a compatible adapter. See [Codex configuration](https://developers.openai.com/codex/config-advanced/).

The wizard offers separate ChatGPT sign-in, OpenAI API-key, and Anthropic API-key connections. Codex handles ChatGPT login and refresh. The Anthropic template can be saved, but activation requires the adapter work described in the compatibility notes.

Provider presets are configuration starting points. A preset or a successful model-list request does not establish that a model supports the complete coding workflow. Model IDs and capabilities should be checked against the selected provider's current catalog.

Model lists are searchable without a fixed result cutoff. Unknown context limits and reasoning options stay unknown. `cmgr model catalog` can export custom Codex labels; it requires an explicit instruction-template file because Codex catalogs also affect instructions. See [configuration](docs/wiki/Configuration.md).

## Files and privacy

By default, the manager uses `~/.codex/`; it respects `CODEX_HOME` when configured.

| File or directory | Purpose |
| --- | --- |
| `cmgr.json` | Manager settings, provider definitions, and any pasted keys |
| `config.toml` | Codex configuration updated by provider switching |
| `sessions/` | Codex session rollouts |
| `backups/` | Copies of configuration, settings, and modified sessions |

Pasted keys are stored as plaintext in local settings and may also appear in config backups. Export removes literal keys, literal HTTP headers and catalog identity by default. Environment references remain for portability. `--include-secrets` explicitly creates a private export with plaintext credentials. Review endpoint names, labels, environment references and custom instruction templates before sharing.

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
