# Compatibility

Codex Manager runs alongside Codex CLI. Configuration and protocol checks currently target Codex 0.159.0. A provider preset is a starting point, not proof of account access or coding capability.

The current source includes guarded configuration changes, separate authentication paths, catalog discovery and validated JSONL session conversion. Codex 0.159.0 parsing and native writer coordination have been checked with synthetic history. Cross-provider inference and protocol adapters still need complete coding-workflow validation.

Codex's native custom transport is Responses. Chat Completions and Anthropic Messages templates are blocked from activation until a compatible adapter is available. Saving such a template does not establish that it can launch a coding session.

Model-list requests can reveal catalog entries without proving inference permission. Verify model IDs, tool support, authentication, context limits, and transport with the selected provider. Manual IDs remain provider-specific.

Older direct Anthropic model versions can be retired while a gateway retains an alias. Check the gateway independently. OpenCode Zen uses different transports for different model families.

See [configuration](Configuration.md), [recovery](Recovery.md), and the provider's current API documentation.

Session conversion handles recognized reasoning in response items, replacement histories, guardian checkpoints and readable inter-agent communication. Encrypted-only compaction/messages/tool results cannot be reconstructed and are rejected. Paginated or inherited stores require a canonical Codex store workflow for conversion, backup and restore; the manager refuses file-only replacements. Other editors and older Codex clients must be stopped before changing saved history.
