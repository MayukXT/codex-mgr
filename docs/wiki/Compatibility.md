# Compatibility

Codex Manager runs alongside Codex CLI. Configuration and protocol checks currently target Codex 0.159.0. A provider preset is a starting point, not proof of account access or coding capability.

The 1.0.0 implementation has known issues in configuration switching, OpenAI credential handoff, cross-provider resumption, backup lookup, and the Anthropic adapter. Until those paths are repaired, use Codex directly for important existing sessions.

The current Anthropic adapter does not implement complete Responses requests, tool calls, or streaming. Several presets still select the obsolete Chat Completions transport. Neither behavior is compatible with current Codex Responses traffic.

Model-list requests can reveal catalog entries without proving inference permission. Verify model IDs, tool support, authentication, context limits, and transport with the selected provider. Manual IDs remain provider-specific.

Older direct Anthropic model versions can be retired while a gateway retains an alias. Check the gateway independently. OpenCode Zen uses different transports for different model families.

See [configuration](Configuration.md), [recovery](Recovery.md), and the provider's current API documentation.
