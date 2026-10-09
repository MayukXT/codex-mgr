# Design Specification & UI Architecture (`design.md`)

*Format: Google Stitch / Modern Terminal UI Design System*  
*Project: Codex Manager (`codex-mgr` / `cmgr`)*  
*Target Environment: Modern Terminal Emulators (Windows Terminal, iTerm2, WezTerm, Ghostty, Alacritty)*

---

## 1. Visual Identity & Aesthetic Principles

### 1.1 Aesthetic Philosophy
`codex-mgr` delivers a state-of-the-art developer experience that feels as polished as a native desktop application. It emphasizes:
- **Instant Clarity:** Visual badges clearly communicate provider bindings (`[OpenAI]`, `[AgentRouter]`, `[OpenRouter]`).
- **Complete User Agency:** Every step asks for confirmation, gives the user freedom to name and customize fields, and provides clear fallback options.
- **Micro-Feedback:** Instant completion badges (`[✓] Backed up`, `[✓] Sanitized 14 traces`, `[✓] Switched`).
- **Graceful Error Recovery:** Never presents a raw Python traceback to non-technical users; all errors are presented in clear, actionable diagnostic boxes.

### 1.2 Color Palette (ANSI & Hex Tokens)
| Token | Hex | Terminal ANSI | Role |
| :--- | :--- | :--- | :--- |
| **`primary.accent`** | `#339CFF` | Cyan / Bright Blue | Title headers, active cursor, primary actions |
| **`provider.openai`** | `#10A37F` | Bright Green | OpenAI official provider badge & success checks |
| **`provider.router`** | `#9333EA` | Magenta / Purple | AgentRouter & OpenRouter gateway badges |
| **`provider.local`** | `#F59E0B` | Amber / Yellow | Local OSS providers (Ollama, LM Studio) |
| **`status.warning`** | `#FBBF24` | Bright Yellow | Conversion alert, running process warning |
| **`status.danger`** | `#EF4444` | Bright Red | Connection failure, invalid key, file corruption |
| **`text.muted`** | `#6B7280` | Dim / Gray | Timestamps, file paths, UUID previews |
| **`surface.border`** | `#374151` | Dark Gray | Panel borders and table dividers |

---

## 2. Chronological Screen Designs

### Screen 1: Main Dashboard (`tui/dashboard.py`)
Displayed on launching `cmgr` without arguments:

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│  🚀  CODEX MANAGER (cmgr)  v1.0.0                                          │
│  The universal multi-provider bridge & session manager for Codex           │
├─────────────────────────────────────────────────────────────────────────────┤
│  ACTIVE PROVIDER : [bold #10A37F]● OpenAI (Official)[/]                                  │
│  DEFAULT MODEL   : [bold #339CFF]gpt-6.1-sol[/] (Effort: [dim]ultra[/])                     │
│  CODEX BINARY    : [dim]C:\...\Codex\bin\9691020b546a15b2\codex.exe[/]             │
│  TOTAL SESSIONS  : 67 sessions recorded (57 OpenAI • 10 Router)             │
╰─────────────────────────────────────────────────────────────────────────────╯

? What would you like to do? (Use arrow keys ⬆/⬇, press Enter)
 ❯ 🚀 Resume a Session        (Auto-bridges reasoning blobs seamlessly)
   🔄 Switch Active Provider  (OpenAI ↔ AgentRouter ↔ Claude 5.5)
   ➕ Manage Providers        (Add, edit, or test API connections)
   ⚡ Start New Session       (Launch fresh session with active provider)
   🩺 Doctor & Health Check   (Verify configs, paths, and connectivity)
   ↩️ Undo / Revert a Session (Restore original session from backup)
   🚪 Exit
```

---

### Screen 2: Interactive Session Picker (`tui/session_picker.py`)
Displayed when choosing **"Resume a Session"**:

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│  Select a Session to Resume                                                │
│  Type to search keywords • [Esc] to go back                                │
╰─────────────────────────────────────────────────────────────────────────────╯

  ORIGIN        THREAD NAME / TOPIC                 LAST ACTIVE      BRIDGE ACTION
───────────────────────────────────────────────────────────────────────────────
❯ [green][OpenAI][/]      Recover encrypted conversations     2 hours ago      [cyan][Direct Resume][/]
  [green][OpenAI][/]      Fix mobile chat flow                Yesterday        [cyan][Direct Resume][/]
  [purple][AgentRouter][/] Fix chat history loading           3 days ago       [yellow]⚡ Auto-Bridge ➔ OpenAI[/]
  [purple][Claude 5.5][/]  Long-horizon refactor with Fable    3 days ago       [yellow]⚡ Auto-Bridge ➔ OpenAI[/]
  [green][OpenAI][/]      Redesign WhatsApp UI with Stitch    Last week        [cyan][Direct Resume][/]
───────────────────────────────────────────────────────────────────────────────
Page 1 of 5 (Showing 1-5 of 67) • Press [Tab] to filter by provider
```

When selecting a session requiring conversion:
```text
╭── ⚡ Cross-Provider Bridge Notice ───────────────────────────────────────────╮
│  This session was created with: [bold purple]AgentRouter[/]                               │
│  Target active provider is    : [bold green]OpenAI (Official)[/]                           │
│                                                                             │
│  • An atomic backup will be created before touching the file.               │
│  • Incompatible reasoning traces will be cleanly bridged.                   │
│  • 100% of your messages, code diffs, and tool runs will be preserved.      │
╰─────────────────────────────────────────────────────────────────────────────╯

? Proceed with conversion and launch Codex? (Y/n): 
```

---

### Screen 3: Step-by-Step Provider Creation Wizard (`tui/provider_wizard.py`)
Guides non-technical users chronologically:

```text
[Step 1 of 6] Choose a Provider Template (Cline / OpenCode style):
 ❯ OpenRouter           (Universal multi-model API gateway)
   Anthropic (Direct)   (Official Claude API - via built-in proxy)
   AgentRouter          (High-speed proxy router)
   DeepSeek             (Official DeepSeek API)
   Groq                 (Ultra-fast LPU inference)
   Google Gemini        (Direct API via OpenAI compatibility)
   Ollama (Local)       (Self-hosted offline models)
   LM Studio (Local)    (Desktop local model runner)
   Custom Endpoint      (Any OpenAI-compatible or Anthropic endpoint)

[Step 2 of 6] Provider Name & Alias:
  Enter a friendly label [OpenRouter]: My Work Router

[Step 3 of 6] Base URL & Wire Protocol:
  Base URL [https://openrouter.ai/api/v1]: https://openrouter.ai/api/v1
  Wire Protocol:
   ❯ responses (Native Codex Responses API - Recommended)
     chat      (Standard /v1/chat/completions)

[Step 4 of 6] Authentication:
  How would you like to provide the API key?
   ❯ Paste API Key now (saved safely in ~/.codex/cmgr.json)
     Reference an Environment Variable (e.g. $OPENROUTER_API_KEY)
  Enter API Key: ****************************************

[Step 5 of 6] Model Discovery & Selection:
  🔍 Probing endpoint for available models (GET /v1/models)...
  [bold green][✓] 38 models discovered on endpoint![/]

  Select default model:
   ❯ anthropic/claude-3.7-sonnet
     openai/gpt-5.6-sol
     deepseek/deepseek-r1
     meta-llama/llama-3.3-70b-instruct
     [+] Enter custom model ID manually...

  Reasoning Effort:
     low
     medium
   ❯ high
     max

[Step 6 of 6] Connection Health Verification:
  [yellow]Testing connection to https://openrouter.ai/api/v1...[/]
  [bold green][✓] Connection successful! (HTTP 200 OK • Ping: 142ms)[/]

╭─────────────────────────────────────────────────────────────────────────────╮
│  [bold green]Provider 'My Work Router' successfully configured![/]                   │
╰─────────────────────────────────────────────────────────────────────────────╯

? Would you like to switch to this provider right now? (Y/n): 
```

---

### Screen 4: Model Management & Custom Models (`tui/model_picker.py`)
Displayed when customizing models for an existing or default preset:

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│  Manage Models for: [bold cyan]AgentRouter[/]                                      │
╰─────────────────────────────────────────────────────────────────────────────╯

  CURRENT MODELS CONFIGURED:
  1. [green]●[/] gpt-5.6-sol               (Active Default • Effort: max)
  2.   claude-3-7-sonnet-thinking  (Effort: high)
  3.   deepseek-r1                 (Effort: max)

? What would you like to do?
 ❯ ➕ Add New Model (Probe endpoint or manual ID)
   ⭐ Set Active Default Model
   ✏️ Edit Model Alias / Reasoning Effort
   🗑️ Remove Model from Provider
   ↩️ Back to Main Menu
```

---

### Screen 5: Settings Backup, Export & Import (`tui/portable_view.py`)

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│  📦  Configuration Portability & Backups                                   │
╰─────────────────────────────────────────────────────────────────────────────╯

? Select an operation:
 ❯ 📤 Export Configuration Bundle (Save all providers & models to JSON)
   📥 Import Configuration Bundle (Load settings from JSON file)
   🕒 View Settings Revision History (Restore ~/.codex/cmgr.json from .bak)
   ↩️ Back

[Export Selected]
? Sanitize / redact sensitive API keys before exporting? [y/N]: N
[bold green][✓] Exported complete setup to: ./cmgr-export-2026-10-09.json[/]
```

---

### Screen 6: Diagnostic Doctor Report (`tui/doctor.py`)
Displayed on running `cmgr doctor`:

```text
╭─────────────────────────────────────────────────────────────────────────────╮
│  🩺  CODEX ENVIRONMENT DIAGNOSTIC REPORT                                   │
╰─────────────────────────────────────────────────────────────────────────────╯

  CHECK COMPONENT             STATUS      DETAILS
───────────────────────────────────────────────────────────────────────────────
  Codex Executable            [bold green]PASSED[/]      v26.1002 (C:\...\9691020b546a15b2\codex.exe)
  Configuration (config.toml) [bold green]PASSED[/]      Valid TOML • 2 custom MCP servers preserved
  Tool Settings (cmgr.json)   [bold green]PASSED[/]      Valid JSON • 6 providers configured • Backup ready
  Active Provider Endpoint    [bold green]PASSED[/]      OpenAI API (Reachable • 94ms)
  Anthropic Adapter Proxy     [bold green]PASSED[/]      Ready on 127.0.0.1:4099 (Idle)
  Session Storage Directory   [bold green]PASSED[/]      67 rollout files found in ~/.codex/sessions
  Reasoning Blobs Health      [bold green]PASSED[/]      All sessions indexed without corruptions
  Backup Storage              [bold green]PASSED[/]      ~/.codex/backups/ initialized (0.4 MB used)
───────────────────────────────────────────────────────────────────────────────
  SYSTEM STATUS: [bold green]ALL CHECKS HEALTHY[/] — Codex is ready for seamless switching!
```

---

## 3. Keyboard Shortcuts & Accessibility
- **`⬆ / ⬇` or `k / j`**: Move selection up/down.
- **`Enter`**: Confirm choice / proceed.
- **`Esc` or `q`**: Back / Cancel (returns cleanly without side effects).
- **`/`**: Quick filter / search sessions or models by keyword.
- **`Ctrl+C`**: Clean interrupt (cleans up any temporary files).

