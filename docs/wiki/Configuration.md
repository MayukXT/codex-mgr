# Configuration and authentication

Codex separates the inference model from its connection:

| Field | Purpose |
| --- | --- |
| `model` | Exact model ID sent to the endpoint |
| `model_provider` | Local connection ID |
| `[model_providers.<id>]` | Endpoint, authentication, and transport |
| Provider `name` | Friendly connection label |

Custom IDs must match `model_provider`. They do not need to be the endpoint's vendor name. Reserved built-in IDs cannot be repurposed as unrelated connections.

A compatible connection needs a model ID and protocol in addition to its endpoint and credentials. Current Codex uses the Responses protocol. Chat Completions and Anthropic Messages services require a compatible adapter.

ChatGPT sign-in and OpenAI API-key authentication are separate methods. Let Codex manage sign-in and refresh. An API key authorizes the selected API endpoint; a subscription does not establish access to an unrelated gateway.

Prefer an environment-variable credential reference over storing a pasted key. Never put a key in an endpoint URL. See [privacy](Security-And-Privacy-Guide.md) and [compatibility](Compatibility.md).

Provider changes preserve comments and unrelated configuration. A selected Codex profile receives model settings. Invalid files and conflicting edits stop the operation; snapshots preserve the prior configuration and manager settings. `cmgr recover` repairs an interrupted change when the files still match the recorded transaction. Review external conflicts before retrying.

`tui.model_availability_nux` stores startup-notice counts, not model access or limits. A model's display name comes from catalog metadata; the desktop app can label a model without that metadata as Custom.

[Official Codex configuration](https://developers.openai.com/codex/config-advanced/).
