# Security, Privacy & Secret Hygiene Guide

This project is built for the public open-source community. Protecting user privacy and preventing accidental leakage of API credentials, chat histories, or machine identities is a mandatory requirement.

## 1. Zero-Leak Credential Policy
- **No Hardcoded Secrets:** Code, tests, and documentation must never contain real API keys, bearer tokens, or personal identifiers.
- **Dynamic Path Resolution:** Paths are resolved strictly via runtime environment variables (`$CODEX_HOME`, `$LOCALAPPDATA`, `~/.codex`). No machine usernames (e.g. personal Windows paths) are ever committed.
- **Key Masking:** In all terminal displays, logs, and tables, API keys are masked by default (`sk-...1234`).
- **Safe Exporting:** The `cmgr export` command includes a `--sanitize-keys` option that strips all actual keys before sharing configuration bundles with colleagues or the public.

## 2. Test Fixture Isolation
- All unit and integration tests utilize synthetic mocks (e.g., `sk-mock-key-1234`, `00000000-0000-0000-0000-000000000000`).
- No real session logs or chat history files from the local user's `.codex` folder are ever copied into the git repository.

## 3. Strict `.gitignore` Protection
The repository `.gitignore` strictly ignores:
- `cmgr.json` and any local settings files
- `*.jsonl`, `*.jsonl.bak*` (conversation session rollouts)
- `*.env`, `*.key`, `auth.json`, `credentials.json`
- `*.sqlite`, `*.sqlite-wal`, `*.sqlite-shm`

## Related Links
- [[Architecture]]
- [[Disaster-Recovery-And-Backups]]
- [[Provider-Compatibility-Matrix]]
