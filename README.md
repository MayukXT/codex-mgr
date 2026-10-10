# Codex Manager

Manage Codex CLI providers, models, and saved sessions from your terminal.

`cmgr` brings provider settings, model selection, session resumption, and recovery tools into one place. It runs alongside [Codex CLI](https://developers.openai.com/codex/cli/) and uses your existing Codex home directory.

## Project status

Compatibility and recovery work is underway for v1.1.0. Named profile migration, cross-provider resumption and protocol adapters still need complete workflow validation before this version can be recommended for everyday use. See [compatibility](docs/wiki/Compatibility.md) for the current limits.

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

The bridge changes saved session files. It validates JSONL history and saves a checksummed snapshot before applying a conversion. Removing encrypted reasoning loses information that cannot be reconstructed from readable summaries. Encrypted-only compaction or communication cannot be converted safely and requires the original connection.

## Commands

```sh
cmgr --help
cmgr                         # Open the terminal menu
cmgr provider add            # Configure a provider
cmgr provider edit my-api    # Edit a saved connection
cmgr provider test my-api    # Check catalog access; no inference request
cmgr model list --refresh    # Load the active connection's catalog
cmgr model use MODEL_ID      # Select an exact model ID
cmgr sessions --search bug   # Find sessions by title or UUID
cmgr sessions --workspace .  # Sessions created in this directory
cmgr switch                  # Show provider IDs and the saved active provider
cmgr doctor                  # Read local diagnostics without changing files
cmgr doctor --network        # Also check API catalog access; no inference request
cmgr start --provider my-api # Apply the connection and start a new Codex session
cmgr recover                 # Recover an interrupted configuration change
cmgr recover --list          # Review pending changes and external conflicts
cmgr resume SESSION_UUID --to my-api
cmgr restore SESSION_UUID --list
cmgr export --output setup.json --sanitize-keys
cmgr import setup.json
cmgr --version
cmgr update --check
```

`cmgr resume` activates the selected connection and model before launching Codex. Conversion that removes encrypted reasoning requires `--allow-reasoning-loss`, or confirmation in the menu. `cmgr restore SESSION_UUID --snapshot FILENAME` selects a listed snapshot and saves current content before restoring it. See [session recovery](docs/wiki/Recovery.md) for supported history formats and writer coordination.

Activation asks Codex to resolve configuration layers and managed requirements in the current working directory. It verifies the effective connection and model before saving the manager's active state. Conflicting project or managed settings stop the change; failed verification rolls back matching files and preserves external edits. Legacy `[profiles]` configurations require explicit migration and are preserved when activation stops.

`cmgr start --provider my-api "INITIAL PROMPT"` uses the same selected credential and activation checks for a fresh session. Prompt text is passed as one literal argument to Codex. On Windows, supported npm command wrappers are resolved to the packaged native executable; unresolved shell wrappers require adding the native executable to PATH. Codex errors and cancellation return a nonzero status.

`cmgr doctor` reports failed, warning and unchecked states separately. It does not establish inference or tool compatibility. Failed diagnostics and unavailable update checks return a nonzero exit code. `cmgr update` installs the checked stable release with the current Python interpreter's pip, verifies its installed version, and asks you to restart. If pip cannot manage that environment, use the package manager that installed it.

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
