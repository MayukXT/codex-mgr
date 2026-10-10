# Codex Manager (`codex-mgr` / `cmgr`)

> **Universal multi-provider manager, session recovery bridge, and terminal dashboard for OpenAI Codex CLI.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Codex CLI Ready](https://img.shields.io/badge/Codex%20CLI-Ready-green.svg)](https://developers.openai.com/codex/cli/)
[![Platform Support](https://img.shields.io/badge/platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey.svg)](#)

---

## 🌟 What is Codex Manager?

**Codex Manager (`cmgr`)** brings provider configuration, model tuning, session resumption, and disaster recovery into a single, intuitive terminal interface. It works seamlessly alongside [Codex CLI](https://developers.openai.com/codex/cli/), using your existing `~/.codex/` directory without disrupting your workflow.

Whether you are switching between cloud providers, routing through high-speed proxies, or running offline local models, `cmgr` ensures your active sessions, conversations, and custom configurations remain intact and fully recoverable.

---

## 💡 Why Do You Need It?

| The Challenge in Codex CLI | How Codex Manager Solves It |
| :--- | :--- |
| **Broken Resumptions:** Switching providers often causes session resumption to fail due to incompatible provider metadata and encrypted reasoning traces. | **Intelligent Session Bridging:** Automatically detects provider changes, creates atomic pre-conversion snapshots, and bridges reasoning traces so 100% of prompts, code diffs, and tool runs survive. |
| **Configuration Overwrites:** Manually editing `config.toml` risks accidentally deleting custom MCP servers, custom hooks, and active theme preferences. | **Zero-Loss Transactional Config:** Surgically updates connection layers while strictly preserving comments, custom MCP servers, and unmanaged settings with automatic rollbacks. |
| **Fragmented Management:** Managing API keys, testing endpoint latency, and locating past sessions requires digging through raw JSON and TOML files. | **Unified Terminal Dashboard (TUI):** A keyboard-driven interactive interface featuring visual menus, a 6-step provider wizard, and instant session search. |
| **Disaster Recovery:** Corrupted sessions or accidental configuration changes can leave you locked out of work. | **Complete Recovery Toolkit:** Built-in transaction reversions, independent history copies, and verified configuration restore tools. |

---

## 🚀 Quick Start

### Installation

Install `cmgr-cli` via `pip` or `pipx`:

```bash
pip install cmgr-cli
# or via pipx:
pipx install cmgr-cli
```

> **Note:** Both `cmgr` (short) and `codex-mgr` (full) commands are available in your shell.

To install directly from source:
```bash
git clone https://github.com/MayukXT/codex-mgr.git
cd codex-mgr
python -m pip install .
```

---

## 🎮 Interactive Terminal Dashboard

Launch the interactive dashboard by running `cmgr`:

```bash
cmgr
```

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│  🚀  CODEX MANAGER (cmgr)                                                  │
│  Universal Multi-Provider Manager & Session Bridge for Codex CLI            │
├─────────────────────────────────────────────────────────────────────────────┤
│  ACTIVE PROVIDER : ● OpenAI (Official)                                      │
│  REASONING EFFORT: High (Adaptive Mode)                                     │
│  TOTAL SESSIONS  : 67 sessions recorded (Ready for resumption)              │
╰─────────────────────────────────────────────────────────────────────────────╯

? What would you like to do? (Use arrow keys ⬆/⬇, press Enter)
 ❯ 🚀 Resume a Session        (Auto-bridges reasoning traces seamlessly)
   🔄 Switch Active Provider  (Switch endpoints without losing sessions)
   ➕ Manage Providers        (Add, edit, or test provider connections)
   ⚡ Start New Session       (Launch fresh session with active provider)
   🩺 Doctor & Diagnostics    (Verify configs, paths, and connectivity)
   📦 Export & Import Setup   (Safely backup, migrate, or share configs)
   🛠️ Recovery & Revisions    (Transactional rollback & history copies)
   🚪 Exit
```

---

## ⚡ Key Features

### 1. 🔄 Universal Session Bridging & Recovery
* **Cross-Provider Resumption:** Safely transition sessions between different backends.
* **Independent History Copies:** Creates verified, independent session copies with fresh identities to ensure original history files are never overwritten.
* **Loss Transparency:** Clearly informs you if provider transitions require shedding proprietary reasoning traces before proceeding.

### 2. 🔌 Comprehensive Provider Management
* **Guided Setup Wizard:** A step-by-step wizard to configure connection templates, endpoints, wire protocols, and credentials.
* **Zero-Token Latency Probing:** Tests network health and queries available remote catalogs using lightweight metadata endpoints without spending prompt tokens.
* **Flexible Authentication:** Supports direct API keys, references to environment variables (e.g. `$MY_API_KEY`), and native sign-in flows.

### 3. 🛡️ Zero-Loss Configuration Safety
* **Comment & Hook Preservation:** Modifies only the designated provider sections in `config.toml`, preserving custom MCP servers, tool definitions, and formatting.
* **Atomic Snapshots:** Every configuration write is preceded by a collision-proof timestamped backup. If validation fails, changes roll back automatically.

### 4. 🩺 Diagnostic Health Check (`cmgr doctor`)
* Run `cmgr doctor` to inspect your Codex environment at a glance:
  * Verification of the Codex CLI executable.
  * Validation of configuration and tool settings schemas.
  * Endpoint latency and reachability testing (`--network`).
  * Integrity checks for saved session directories and reasoning blobs.

### 5. 📦 Portability & Safe Sharing
* **Redacted Bundles:** Export your entire provider setup to JSON (`cmgr export`) with API keys automatically sanitized for safe sharing.
* **Seamless Import:** Restore or migrate your settings to a new machine in seconds (`cmgr import`).

---

## 🔌 Supported Provider Types

Codex Manager connects to any provider architecture supported by Codex CLI or compatible wire adapters:

* **Official OpenAI:** Managed sign-in and direct API key authentication.
* **Anthropic Services:** Protocol-adapted transport for native thinking, tool streaming, and completions.
* **Universal Gateways & Routers:** Multi-model routing gateways, custom proxy endpoints, and load balancers.
* **Third-Party APIs:** Direct provider endpoints speaking OpenAI-compatible protocols.
* **Local & Self-Hosted:** Offline, private inference engines running on your local machine (e.g., local server runners and offline gateways).

---

## 💻 Command-Line Reference

Power users and automation scripts can execute commands directly:

```bash
# === Launch & Interactive ===
cmgr                             # Open the interactive TUI menu
cmgr start --provider my-router  # Start a fresh session with a specified provider
cmgr resume --last               # Resume the most recent session
cmgr resume <SESSION_UUID>       # Resume a specific session by UUID

# === Provider Management ===
cmgr switch                      # List saved providers and current active selection
cmgr switch my-router            # Switch active provider directly
cmgr provider add                # Launch the step-by-step provider creation wizard
cmgr provider test my-router     # Test connection latency without burning tokens
cmgr provider edit my-router     # Update base URL, headers, or credentials

# === Model Selection & Settings ===
cmgr model list --refresh        # Refresh and inspect the active provider's catalog
cmgr model use <MODEL_ID>        # Select an exact model ID from the catalog

# === Diagnostics & Recovery ===
cmgr doctor                      # Run the diagnostic health check
cmgr doctor --network            # Health check with network latency testing
cmgr sessions --search "refactor" # Search past sessions by title or UUID
cmgr recover                     # Recover from interrupted transactions or conflicts
cmgr revisions list              # List verified configuration snapshots
cmgr history-copies list         # Inspect independent recovery session bundles

# === Configuration Backup & Portability ===
cmgr export --output setup.json  # Export settings with API keys sanitized
cmgr import setup.json           # Import configuration bundle into local environment
```

---

## 🔒 Security & Privacy Policy

* **Zero-Leak Credential Masking:** API keys are masked in the terminal interface (`sk-...1a2b`) and never printed in logs or diagnostic reports.
* **Sanitized Exports by Default:** Exported configuration bundles strip plain-text credentials and sensitive HTTP headers unless explicitly requested.
* **Atomic Pre-Mutation Backups:** All settings and session rollout files are backed up to `~/.codex/backups/` before any write operation.
* **100% Local Execution:** No chat transcripts, prompts, or personal tokens are ever transmitted to any third-party tracking or telemetry server.

---

## 📄 License

Distributed under the [MIT License](LICENSE).
