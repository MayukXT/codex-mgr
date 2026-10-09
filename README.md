# Codex Manager (`codex-mgr` / `cmgr`)

> **Universal multi-provider manager and reasoning-blob session bridge for OpenAI Codex CLI.**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![Codex CLI](https://img.shields.io/badge/Codex-2026%20Ready-green.svg)](https://openai.com)

---

## 🌟 Overview

When switching OpenAI Codex CLI between model providers (such as official OpenAI access, AgentRouter, OpenRouter, Anthropic Claude 5.5, DeepSeek, or local Ollama), session resumption typically breaks. 

This happens because OpenAI models output proprietary Fernet-encrypted reasoning blobs (`encrypted_content`), which upstream routers reject with `HTTP 400 Bad Request`, and local Codex state reducers reject with identity mismatch errors.

`codex-mgr` (`cmgr`) eliminates this friction entirely. It provides:
1. **Interactive Terminal Dashboard (TUI):** A single unified arrow-key menu to switch providers and resume sessions.
2. **Universal Session Bridging:** Automatically detects provider changes, creates atomic backups, and sanitizes reasoning traces in $<0.1$s without losing any prompts, code diffs, or tool outputs.
3. **2026 Model Generation Support:** Native out-of-the-box presets for **GPT-6.1 Sol**, **GPT-6 Astra**, **Claude Sonnet 5.5**, **Claude Opus 5.5**, **Claude Fable 5.1**, **DeepSeek R2**, and local offline inference.
4. **Local Model Cache Ingestion:** Automatically reads `~/.codex/models_cache.json` to keep available models current.
5. **Zero-Token Health Probe:** Tests connections and lists available models using metadata endpoints without spending prompt tokens.
6. **Built-in Anthropic Proxy:** Micro-adapter enabling native Claude endpoints to speak Codex's Responses wire protocol.
7. **Zero-Loss Config Switching:** Surgically updates `config.toml` while strictly preserving custom MCP servers (`kaggle`, `firecrawl`), hooks, and themes.

---

## 🚀 Installation

Install via `pip` or `pipx`:

```bash
pip install codex-mgr
# or
pipx install codex-mgr
```

This installs both `cmgr` (short) and `codex-mgr` (full) commands.

---

## 🎮 Interactive TUI Dashboard

Simply type `cmgr` in any terminal:

```bash
cmgr
```

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│  🚀  CODEX MANAGER (cmgr)  v1.0.0                                          │
│  The universal multi-provider bridge & session manager for Codex           │
├─────────────────────────────────────────────────────────────────────────────┤
│  ACTIVE PROVIDER : ● OpenAI (Official)                                      │
│  DEFAULT MODEL   : gpt-6.1-sol (Effort: ultra)                              │
│  TOTAL SESSIONS  : 67 sessions recorded (57 OpenAI • 10 Router)             │
╰─────────────────────────────────────────────────────────────────────────────╯

? What would you like to do? (Use arrow keys ⬆/⬇, press Enter)
 ❯ 🚀 Resume a Session        (Auto-bridges reasoning blobs seamlessly)
   🔄 Switch Active Provider  (OpenAI ↔ AgentRouter ↔ Claude 5.5)
   ➕ Manage Providers        (Add, edit, or configure models)
   ⚡ Start New Session       (Launch fresh session with active provider)
   🩺 Doctor & Health Check   (Verify configs, paths, and connectivity)
   📦 Export & Import Setup   (Backup, share, or migrate configurations)
   ↩️ Undo / Revert Session   (Restore original session from backup)
   🚪 Exit
```

---

## ⚡ Direct CLI Usage

Power users and scripts can use direct commands:

```bash
# Resume most recent session (auto-bridging if needed)
cmgr resume --last

# Resume specific session by title or UUID
cmgr resume "Recover encrypted"

# Switch active provider instantly
cmgr switch agentrouter
cmgr switch openai

# Check version & query upstream releases
cmgr version

# Check for updates without upgrading
cmgr update --check

# Self-update to the latest version via pip
cmgr update

# Run diagnostic health check
cmgr doctor

# Add a provider with the 6-step guided wizard
cmgr provider add

# Export configuration (safely redact keys for sharing)
cmgr export --output my-setup.json --sanitize-keys

# Import configuration bundle
cmgr import my-setup.json

# Restore a session to its original unbridged backup
cmgr restore 019ee539-f672-7a12-acb7-2c2b614c1498
```

---

## 🔒 Security & Privacy Policy

- **Zero-Leak Guarantee:** Real API keys are never written to logs or displayed unmasked.
- **Key Masking:** Credentials are automatically masked in the UI (`sk-...1a2b`).
- **Atomic Backups:** All files are backed up to `~/.codex/backups/` before modification.
- **Local Isolation:** No chat history or tokens are sent to any external tracking or telemetry server.

---

## 📄 License

MIT License. See [LICENSE](LICENSE) for details.
