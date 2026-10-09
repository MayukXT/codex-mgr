# Contributing

Install Python 3.10 or later, Git, and Codex CLI.

```sh
git clone https://github.com/MayukXT/codex-mgr.git
cd codex-mgr
python -m venv .venv
```

Activate `.venv\Scripts\Activate.ps1` in PowerShell or `source .venv/bin/activate` on macOS/Linux, then install:

```sh
python -m pip install -e ".[dev]"
python -m pytest tests -q
```

## Package structure

- `src/codex_mgr/config/`: validated settings and portable bundles.
- `src/codex_mgr/core/`: provider configuration, catalogs, session conversion, recovery, diagnostics, and launch.
- `src/codex_mgr/tui/`: terminal menus using the same core operations as the CLI.
- `src/codex_mgr/utils/`: paths, formatting, input normalization, and file helpers.
- `tests/`: synthetic fixtures and protocol tests.

## Change requirements

Preserve Codex configuration fields the manager does not own. Parse and validate before mutation, require a successful snapshot, and report failures without changing the saved active provider. Session conversion must preserve conversation and tool records; document any removal of encrypted reasoning.

Keep tests independent of the developer's Codex home, sessions, credentials, and accounts. Mock network traffic and subprocess handoffs. Test malformed input, interrupted writes, conflicting writers, long names, and both CLI and TUI entry points when relevant.

Never include real keys, authentication files, exported settings, session transcripts, local paths, or development notes in fixtures or release archives. Use unmistakably synthetic values and inspect staged files before committing.

## Compatibility

Check emitted fields and transport against the supported Codex configuration schema. Do not infer model capability from its name or treat endpoint reachability as authentication/inference success. A new provider requires a tested model, authentication method, and complete request/response workflow.

Run the complete test suite before submitting a change. Public documentation should describe observable behavior, requirements, and limits.
