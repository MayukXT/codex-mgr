# Configuration and authentication

Codex separates the inference model from its connection:

| Field | Purpose |
| --- | --- |
| `model` | Exact model ID sent to the endpoint |
| `model_provider` | Local connection ID |
| `[model_providers.<id>]` | Endpoint, authentication, and transport |
| Provider `name` | Friendly connection label |

Custom IDs must match `model_provider`. They do not need to be the endpoint's vendor name. Reserved built-in IDs cannot be repurposed as unrelated connections.

If saved connections share a Codex ID, the saved active connection resolves that identity. An ambiguous ID without an active match stops loading instead of guessing. Native endpoint, transport and authentication fields replace stale imported values; a connection without an explicit model still needs model selection. Credentials may be supplied through a configured key or literal/environment HTTP headers.

A compatible connection needs a model ID and protocol in addition to its endpoint and credentials. Current Codex uses the Responses protocol. Chat Completions and Anthropic Messages services require a compatible adapter.

ChatGPT sign-in and OpenAI API-key authentication are separate methods. Let Codex manage sign-in and refresh. An API key authorizes the selected API endpoint; a subscription does not establish access to an unrelated gateway.

Prefer an environment-variable credential reference over storing a pasted key. Never put a key in an endpoint URL. See [privacy](Security-And-Privacy-Guide.md) and [compatibility](Compatibility.md).

Provider changes preserve comments and unrelated configuration. A selected Codex profile receives model settings. Invalid files and conflicting edits stop the operation; snapshots preserve the prior configuration and manager settings. `cmgr recover` repairs an interrupted change when the files still match the recorded transaction. Review external conflicts before retrying.

`tui.model_availability_nux` stores startup-notice counts, not model access or limits. A model's display name comes from catalog metadata; the desktop app can label a model without that metadata as Custom.

`cmgr model list --refresh` reads the selected connection's catalog. ChatGPT connections use Codex's model API, including its account-aware cache behavior. API connections use their own model endpoint and credentials. Catalog access, inference access and tool compatibility are separate checks. Manual IDs have no assumed context or reasoning capabilities.

`cmgr doctor` reads local configuration, selected credentials, session metadata and recovery status. It creates no settings or backup files. `--network` also requests an API connection's model catalog; ChatGPT sign-in is checked through Codex. A parsed local file does not prove that managed settings or invocation overrides match, and catalog access does not prove a coding workflow. Reports mark those checks explicitly. A failed check returns exit code 1; warnings and unchecked states remain visible without claiming readiness.

To supply custom model labels to Codex 0.159, add named models with `cmgr model add`, then use `cmgr model catalog --output catalog.json --instructions-file instructions.md`. Point `model_catalog_json` in Codex configuration to that file. Review the instruction template: this catalog controls instructions as well as labels. The export preserves existing files and does not install itself automatically.

[Official Codex configuration](https://developers.openai.com/codex/config-advanced/).
