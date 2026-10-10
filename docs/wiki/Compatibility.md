# Compatibility

Codex Manager runs alongside Codex CLI. Configuration and protocol checks currently target Codex 0.159.0. A provider preset is a starting point, not proof of account access or coding capability.

The current source includes guarded configuration changes, separate authentication paths, catalog discovery and validated JSONL session conversion. Codex 0.159.0 parsing and native writer coordination have been checked with synthetic history. Cross-provider inference and protocol adapters still need complete coding-workflow validation.

Codex's native custom transport is Responses. `cmgr start` and the menu's new-session action can launch Chat Completions and Anthropic Messages connections through an owned local adapter. The manager verifies native policy and temporary routing before saving the original upstream selection. It leaves native `config.toml` unchanged and closes the adapter when Codex exits. Persistent `cmgr switch`, shared resumption and complete protocol coverage for these transports remain gated.

Synthetic native checks cover exact text, function/custom-tool results and adapter ownership. They do not establish access to a live provider account or every model's dialect. Unsupported request fields and opaque history fail rather than being silently discarded. Messages thinking currently requires an explicit supported budget and native reasoning effort; model-specific adaptive thinking and effort mappings still need validation. A model that requires those newer controls is not covered by the budget-only path.

Model-list requests can reveal catalog entries without proving inference permission. Verify model IDs, tool support, authentication, context limits, and transport with the selected provider. Manual IDs remain provider-specific.

Older direct Anthropic model versions can be retired while a gateway retains an alias. Check the gateway independently. OpenCode Zen uses different transports for different model families.

See [configuration](Configuration.md), [recovery](Recovery.md), and the provider's current API documentation.

Session conversion handles recognized reasoning in response items, replacement histories, guardian checkpoints and readable inter-agent communication. Encrypted-only compaction/messages/tool results cannot be reconstructed and are rejected. The offline catalog discovers bounded plain and compressed rollout headers. Paginated, inherited or compressed stores require a canonical Codex store workflow for conversion, backup and restore; the manager refuses file-only replacements. Other editors and older Codex clients must be stopped before changing saved history.
