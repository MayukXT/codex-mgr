# Developer & Contributor Guide (`FORDEV.md`)

Welcome to the **Codex Manager (`codex-mgr` / `cmgr`)** developer documentation.

---

## 🛠️ Architecture & Package Layout

```
codex-mgr/
├── pyproject.toml              # Build & dependency metadata, CLI entrypoints (cmgr, codex-mgr)
├── README.md                   # User-facing open-source guide
├── FORDEV.md                   # Contributor & developer workflows
├── design.md                   # Terminal UI specification & design tokens
├── src/
│   └── codex_mgr/
│       ├── cli.py              # CLI argument parser & command dispatcher
│       ├── config/             # Settings (cmgr.json), Pydantic schemas, import/export
│       ├── core/               # Bridge engine, provider catalog, models, proxy, runner
│       ├── tui/                # Interactive terminal user interface screens
│       └── utils/              # Paths, normalizer, display, process, and TOML writer
├── docs/
│   ├── obsidian/               # Obsidian Knowledge Base with bidirectional links
│   └── superpowers/specs/      # Formal specification documents
└── tests/                      # Pytest suite with 100% synthetic mock fixtures
```

---

## 💻 Local Setup & Development

### 1. Prerequisites
- Python 3.10+ (tested up to Python 3.14)
- Git

### 2. Environment Setup
```bash
# Clone the repository
git clone https://github.com/user/codex-mgr.git
cd codex-mgr

# Create and activate virtual environment
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Linux / macOS:
source .venv/bin/activate

# Install in editable mode with development dependencies
pip install -e ".[dev]"
```

### 3. Running the Test Suite
```bash
pytest tests -v
```

---

## 🔒 Secret Hygiene & Public Safety (Strict)

When contributing code, tests, or documentation:
1. **Never commit live API keys or tokens.** Always use synthetic mock tokens in tests (e.g. `sk-mock-key-1234`).
2. **Never commit personal machine paths.** Use `get_codex_home()` and standard path resolution helpers.
3. **Never commit real user session rollouts (`.jsonl`).** Test fixtures must be generated programmatically with synthetic data.
4. **All secrets in UI displays must be masked** using `mask_api_key()`.

---

## 🧠 Core Engineering Concepts

### 1. Encrypted Reasoning Blobs & The Session Bridge
OpenAI models output proprietary Fernet-encrypted reasoning blobs in `response_item` (`encrypted_content`). When switching to non-OpenAI gateways:
- `core/bridge.py` stream-parses the session rollout.
- Sets `encrypted_content = None` on reasoning items and subagent communication frames.
- Re-binds `session_meta.payload.model_provider` to the target provider.
- All writes are transactional via `.tmp` file replacement with pre-modification backups stored in `~/.codex/backups/sessions/`.

### 2. Surgical `config.toml` Patching
`core/profiles.py` uses `dump_toml` to surgically update `model`, `model_provider`, and `[model_providers.<id>]`.
- Custom MCP servers (`[mcp_servers.*]`), desktop theme settings, and security hook hashes are 100% preserved.

### 3. Anthropic 5.5 Adapter Proxy
`core/proxy.py` runs a lightweight loopback server on `127.0.0.1:4099` that translates OpenAI Responses/Chat requests into Anthropic Messages payloads, mapping reasoning efforts (`low` through `ultra`) into adaptive thinking budgets.

---

## 🤝 Adding a New Provider Template
To add a new built-in provider preset:
1. Edit `src/codex_mgr/core/providers.py`.
2. Add a new `ProviderConfig` to `get_default_presets()`.
3. Specify `id`, `name`, `base_url`, `wire_api` (`responses` | `chat` | `anthropic_proxy`), and default models.
4. Add corresponding tests in `tests/test_providers.py`.
