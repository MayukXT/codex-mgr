# Version & Auto-Update Subsystem

## Overview
`codex-mgr` provides built-in version introspection and self-update capabilities directly from the command line and the interactive TUI.

## Commands
```bash
# Check installed version and query PyPI for newer releases
cmgr version

# Check for updates without modifying files
cmgr update --check

# Self-update to the latest release via pip
cmgr update
```

## How It Works (`core/version.py`)
1. **Local Version:** Reads `__version__` defined in `codex_mgr/__init__.py`.
2. **Upstream Release Probing:** Queries `https://pypi.org/pypi/codex-mgr/json` via `httpx` with a short 3.5s timeout.
3. **Semantic Version Comparison:** Uses `packaging.version.parse` (with resilient numeric tuple fallback) to determine whether an update is available.
4. **Self-Update Handoff:** Executes `[sys.executable, "-m", "pip", "install", "--upgrade", "codex-mgr"]` with live terminal progress.

## Cross-Platform Roadmap
- **Current Support (v1.0.0):** Windows (with full PowerShell, CMD, Windows Terminal, and AppData binary auto-detection).
- **Future Support (v1.1.0+):** Linux & macOS environments (standardizing on `$XDG_CONFIG_HOME`, systemd services for the Anthropic proxy, and Homebrew integration).

## Related Links
- [[Architecture]]
- [[Security-And-Privacy-Guide]]
- [[Disaster-Recovery-And-Backups]]
