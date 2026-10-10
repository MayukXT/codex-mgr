# Configuration and authentication

Codex separates the inference model from its connection:

| Field | Purpose |
| --- | --- |
| `model` | Exact model ID sent to the endpoint |
| `model_provider` | Local connection ID |
| `[model_providers.<id>]` | Endpoint, authentication, and transport |
| Provider `name` | Friendly connection label |

Custom IDs must match `model_provider`. They do not need to be the endpoint's vendor name. Codex 0.159 rejects custom definitions using built-in IDs such as `openai`, `ollama` and `lmstudio`; choose a separate custom ID. Imported explicit native IDs remain exact, while new manager connections using those names get a separate generated ID. See the [native provider validation](https://github.com/openai/codex/blob/rust-v0.159.0/codex-rs/config/src/config_toml.rs).

If saved connections share a Codex ID, the saved active connection resolves that identity. An ambiguous ID without an active match stops loading instead of guessing. Native endpoint, transport and authentication fields replace stale imported values; a connection without an explicit model still needs model selection. Credentials may be supplied through a configured key or literal/environment HTTP headers.

A compatible connection needs a model ID and protocol in addition to its endpoint and credentials. Current Codex uses the Responses protocol. Chat Completions and Anthropic Messages services require a compatible adapter.

Fresh Chat and Messages sessions use temporary routing through `cmgr start` or the menu's new-session action. The saved active connection remains the original upstream endpoint, credentials and model. Reloading manager settings preserves that selection instead of importing the unchanged native base selection over it. Running Codex directly uses its native configuration; run it through the manager to use the adapter. See [compatibility](Compatibility.md) for supported controls and current resume limits.

Use **Model settings → Thinking mode** for a Messages connection. These settings also live in its entry in `cmgr.json`:

| `messages_thinking` | Behavior |
| --- | --- |
| `adaptive` | Model decides when to think; no manual budget |
| `enabled` | Manual thinking; `reasoning_budget` must be at least 1,024 and below the output cap |
| `disabled` | Thinking off where the model supports it |
| `between_tools` | No up-front thinking; requires supported between-tool behavior and effort no higher than `high` |
| `null` or omitted | Existing budget controls; active thinking requires an explicit budget and native effort |

With an explicit mode, `reasoning_effort` is independent of thinking. `low`, `medium`, `high`, `xhigh` and `max` map to the same upstream effort where supported. **Model default** removes the override; native `none` also sends no upstream effort override and does not turn off an explicit thinking mode. Set the mode to `disabled` to request off. Stored budgets remain available when switching modes but are sent only in manual mode. Known capability restrictions are validated before saving and again for the model in each request. The Messages output default is 8,192 tokens, reduced when a fresh reported model ceiling is lower.

Set **Summaries** to Shown (`messages_summary = "auto"`) to display supplied summaries, or Hidden (`none`, the default). Hidden/redacted blocks are never expanded into readable text. Disabled mode requires Hidden; between-tools uses local display filtering without sending an upstream display option. Start through `cmgr start`; the launch uses temporary native controls and leaves `config.toml` unchanged. Full preserved-thinking/account enforcement remains limited as described in [compatibility](Compatibility.md).

Model capabilities come from the scoped catalog or explicit manually reviewed metadata. Optional manual constraints `forced_tools_supported`, `sampling_default_only` and `disabled_high_effort_supported` describe conditional Messages behavior that aggregate thinking support does not establish. Configure them only from the endpoint's actual contract. Known exact model IDs use reviewed conditional rules only when granular mode support is fresh; gateway aliases remain unverified. The adapter refuses unverified forced-tool, sampling and disabled high-effort combinations instead of guessing.

ChatGPT sign-in and OpenAI API-key authentication are separate methods. Let Codex manage sign-in and refresh. An API key authorizes the selected API endpoint; a subscription does not establish access to an unrelated gateway.

Prefer an environment-variable credential reference over storing a pasted key. Never put a key in an endpoint URL. See [privacy](Security-And-Privacy-Guide.md) and [compatibility](Compatibility.md).

Provider changes preserve comments and unrelated configuration. Activation reads Codex's effective configuration and managed requirements in the current working directory, then verifies the requested model, connection, endpoint, authentication fields and controls after writing the base configuration. It commits manager state only after verification. This check sends no inference request and does not prove that a model can complete a coding task.

Conflicting project or managed settings stop activation. Failed verification rolls back files that still match the recorded transaction; external edits are preserved. `cmgr recover` checks interrupted changes. Recovery records and their preimages must resolve inside the Codex home's backup directory and pass format/integrity checks before recovery uses them.

Current Codex profiles use separate `NAME.config.toml` files selected with `codex --profile NAME`. Manager activation currently targets the base configuration; named profile activation and explicit migration remain pending. Legacy `profile`/`[profiles]` layouts stop activation with a migration message. They are not silently flattened or removed. See [official configuration precedence](https://learn.chatgpt.com/docs/config-file/config-basic).

`tui.model_availability_nux` stores startup-notice counts, not model access or limits. A model's display name comes from catalog metadata; the desktop app can label a model without that metadata as Custom.

`cmgr model list --refresh` reads the selected connection's catalog. ChatGPT connections use Codex's model API, including its account-aware cache behavior. API connections use their own model endpoint and credentials. Catalog access, inference access and tool compatibility are separate checks. Manual IDs have no assumed context or reasoning capabilities.

Saved catalog capabilities require matching provenance, connection settings and credentials, with a timestamp no more than one hour old. Stale entries keep their IDs and labels; refresh them before relying on their context, reasoning or modality hints. A connection change during discovery leaves the previous catalog intact. Manually maintained entries survive refresh, including their labels and explicit metadata. In Model settings, choose **Model default** to remove an explicit reasoning override.

`cmgr doctor` reads local configuration, selected credentials, session metadata and recovery status. It creates no settings or backup files. `--network` also requests an API connection's model catalog; ChatGPT sign-in is checked through Codex. A parsed local file does not prove that managed settings or invocation overrides match, and catalog access does not prove a coding workflow. Reports mark those checks explicitly. A failed check returns exit code 1; warnings and unchecked states remain visible without claiming readiness.

To supply custom model labels to Codex 0.159, add named models with `cmgr model add`, then use `cmgr model catalog --output catalog.json --instructions-file instructions.md`. Point `model_catalog_json` in Codex configuration to that file. Review the instruction template: this catalog controls instructions as well as labels. The export preserves existing files and does not install itself automatically.

[Official Codex configuration](https://developers.openai.com/codex/config-advanced/).
