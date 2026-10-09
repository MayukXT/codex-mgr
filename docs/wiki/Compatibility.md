# Compatibility

Codex Manager runs alongside Codex CLI. Configuration and protocol checks currently target Codex 0.159.0. A provider preset is a starting point, not proof of account access or coding capability.

The current source includes guarded configuration changes, separate authentication paths and catalog discovery. Cross-provider session resumption and protocol adapters still need full workflow validation. Use Codex directly for important existing sessions until those paths meet the release checks.

Codex's native custom transport is Responses. Chat Completions and Anthropic Messages templates are blocked from activation until a compatible adapter is available. Saving such a template does not establish that it can launch a coding session.

Model-list requests can reveal catalog entries without proving inference permission. Verify model IDs, tool support, authentication, context limits, and transport with the selected provider. Manual IDs remain provider-specific.

Older direct Anthropic model versions can be retired while a gateway retains an alias. Check the gateway independently. OpenCode Zen uses different transports for different model families.

See [configuration](Configuration.md), [recovery](Recovery.md), and the provider's current API documentation.
