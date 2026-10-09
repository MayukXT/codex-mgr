# Design Specification: Codex Manager (`codex-mgr` / `cmgr`)

**Date:** 2026-10-09  
**Status:** In Review  
**Target Audience:** General non-technical users and developers using OpenAI Codex CLI across multiple AI model providers.

---

## 1. Executive Summary

`codex-mgr` (`cmgr`) is an open-source Python CLI and interactive TUI utility designed to eliminate vendor lock-in and session resumption failures in OpenAI Codex CLI. It enables users to switch between model providers (OpenAI, AgentRouter, OpenRouter, DeepSeek, Local Ollama, LM Studio, or custom proxies) and resume any previous conversation session regardless of which provider originally created it.

The tool solves two core technical obstacles:
1. **Proprietary Encrypted Reasoning Blobs:** OpenAI models generate Fernet-encrypted reasoning blobs (`encrypted_content`), which crash third-party APIs with `400 Bad Request` and fail Codex's internal reducer when switching providers.
2. **Provider Lock in Session Metadata:** Codex sessions are stamped with `session_meta.model_provider`. Resuming under a mismatched provider causes validation errors.

`codex-mgr` bridges sessions adaptively, creates automatic backups, preserves all user configuration (MCP servers, hooks, and themes), and offers a frictionless interactive menu with zero manual JSON/TOML editing.

---

## 2. Key Requirements & Features

### 2.1 Generalization & Extensibility
- **No Hardcoded Machine Paths:** Auto-discovers `CODEX_HOME` and `codex.exe` across Windows, macOS, and Linux.
- **Universal Provider Registry:** Supports any model provider with customizable:
  - Base URL
  - Wire API (`responses` vs `chat`)
  - Authentication (Bearer token, environment variable, or official OAuth)
  - Default model & reasoning effort
- **Presets Included:** Out-of-the-box presets for OpenAI, AgentRouter, OpenRouter, DeepSeek, Groq, Ollama, LM Studio.

### 2.2 Failproof Session Bridging
- **Atomic Snapshots:** Every modified session is backed up to `~/.codex/backups/sessions/<session_id>/<timestamp>.jsonl` before modification.
- **Bi-directional Normalization:**
  - *OpenAI $\to$ Third-Party:* Strips `encrypted_content` from reasoning blocks and subagent synthesis frames. Retains 100% of user prompts, assistant text, and tool calls.
  - *Third-Party $\to$ OpenAI:* Purges untrusted/synthetic reasoning IDs to prevent Codex reducer identity collisions.
  - *Universal:* Updates `session_meta.model_provider` to match target provider.
- **One-Click Revert:** Instant rollback command to restore original sessions from backup.

### 2.3 User Experience
- **Interactive TUI Dashboard:** Running `cmgr` presents an arrow-key navigable dashboard showing current active provider, model, and recent sessions with human-friendly thread names.
- **Provider Management Wizard:** Step-by-step interactive prompt to add, edit, or test providers.
- **Power CLI Mode:** Direct commands like `cmgr resume [id|--last]`, `cmgr switch <provider>`, `cmgr doctor`.

### 2.4 Safety & Configuration Preservation
- **Surgical Config Patching:** Swapping providers updates only provider and model blocks in `config.toml`, preserving custom MCP servers (`kaggle`, `firecrawl`), hook security hashes, and desktop theme preferences.

---

## 3. Architecture & Data Flow

```
+-------------------------------------------------------------+
|                      User Invocation                        |
|        Interactive TUI (`cmgr`) or Direct CLI Commands      |
+-------------------------------------------------------------+
                              |
                              v
                +----------------------------+
                |      CLI Dispatcher        |
                |   (`codex_mgr/cli.py`)     |
                +----------------------------+
                              |
         +--------------------+--------------------+
         |                                         |
         v                                         v
+------------------+                     +-------------------+
|  TUI Dashboard   |                     |   Direct Actions  |
| (Arrow-key menu) |                     | (resume / switch) |
+------------------+                     +-------------------+
         \                                         /
          \                                       /
           v                                     v
+-------------------------------------------------------------+
|                     Core Engine Layer                       |
|                                                             |
|  [Provider Registry] <---> [Profile Manager] (config.toml)  |
|  [Session Resolver]  <---> [Session Bridge Engine]          |
|  [Backup Manager]    <---> [Codex Runner Process Handoff]   |
+-------------------------------------------------------------+
                              |
                              v
                 OpenAI Codex Interactive TUI
```

---

## 4. Packaging & Open Source Distribution

- Built with modern `pyproject.toml` standards (compatible with `pip`, `pipx`, and `uv`).
- Published under the MIT License.
- Provides dual entry points: `cmgr` (short) and `codex-mgr` (descriptive).
- Includes Windows launcher wrappers (`cmgr.cmd`) for native Windows Command Prompt / PowerShell usage.
