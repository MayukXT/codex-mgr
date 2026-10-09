# Privacy

The manager uses the Codex home directory selected by `CODEX_HOME`, or `~/.codex` by default.

Pasted keys are stored as plaintext in `cmgr.json` and can appear in configuration or backups. Prefer environment-variable references where possible. Backups and session files can contain private source code and conversations.

Exports currently include credentials unless `--sanitize-keys` is supplied. That option removes the dedicated token field; review URLs and other fields before sharing any bundle.

```sh
cmgr export --output setup.json --sanitize-keys
```

Provider checks, model discovery, and update checks use the network. Running Codex sends prompts and code to the selected inference endpoint. The manager does not make every provider local or offline.

Never share `auth.json`, private keys, `.env`, session rollouts, unsanitized settings, or backups. Ignore rules reduce accidental additions but do not remove data from Git history.
