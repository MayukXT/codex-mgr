# Privacy

The manager uses the Codex home directory selected by `CODEX_HOME`, or `~/.codex` by default.

Pasted keys are stored as plaintext in `cmgr.json` and can appear in configuration or backups. Prefer environment-variable references where possible. Backups and session files can contain private source code and conversations.

Exports remove literal keys, literal HTTP headers and catalog identity by default. Environment-variable references remain. Use `--include-secrets` only for an intended private copy with plaintext credentials. Review endpoint names, labels and environment references before sharing. Custom model catalogs contain the supplied instruction template; review that file separately.

```sh
cmgr export --output setup.json --sanitize-keys
```

Provider checks, model discovery, and update checks use the network. Running Codex sends prompts and code to the selected inference endpoint. The manager does not make every provider local or offline.

Never share `auth.json`, private keys, `.env`, session rollouts, unsanitized settings, or backups. Ignore rules reduce accidental additions but do not remove data from Git history.

Export preserves existing files unless `--overwrite` is explicitly supplied and refuses Codex runtime files or backup directories. Imports validate the bundle version and matching connection IDs. Merge keeps existing settings and adds distinct models; rename creates independent IDs; overwrite applies active settings through the guarded configuration service. A sanitized overwrite keeps existing credentials where the authentication methods are compatible.
