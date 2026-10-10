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

An optional native contract uses Codex 0.159.0 with temporary Codex and SQLite homes, synthetic session history and a loopback fixture endpoint. It reads converted history and checks writer ownership without making an inference request. With that Codex version installed, set `CMGR_NATIVE_CONTRACTS=1` for the test process and run `python -m pytest tests/test_native_codex.py -q`. The default suite skips this contract when the variable is absent.

## Build verification

```sh
python -m build --outdir dist
python scripts/check_distribution.py --install-smoke dist/cmgr_cli-1.0.0-py3-none-any.whl dist/cmgr_cli-1.0.0.tar.gz
```

Use the filenames generated for the current version. The checker rejects unexpected archive paths, links, duplicate members and selected credential/private-home patterns. It verifies package metadata and both command entry points. `--install-smoke` installs each archive in a separate temporary virtual environment, then checks imports, `cmgr` and `codex-mgr` without using a real Codex home. Installation downloads dependencies; it makes no inference request. Review release content as well, since pattern checks cannot identify every form of private information.

GitHub Actions runs regression and archive/install checks on Windows, Linux and macOS with Python 3.10, 3.12 and 3.14. Separate jobs run the pinned Codex native contracts on each platform. A configured workflow is not evidence that its remote jobs passed; check their results before release.
