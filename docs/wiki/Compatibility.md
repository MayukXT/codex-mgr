# Compatibility

Codex Manager runs alongside Codex CLI. Configuration and protocol checks currently target Codex 0.159.0. A provider preset is a starting point, not proof of account access or coding capability.

The current source includes guarded configuration changes, separate authentication paths, catalog discovery and validated JSONL session conversion. Codex 0.159.0 parsing and native writer coordination have been checked with synthetic history. Cross-provider inference and protocol adapters still need complete coding-workflow validation.

Codex's native custom transport is Responses. `cmgr start` and the menu's new-session action can launch Chat Completions and Anthropic Messages connections through an owned local adapter. The manager verifies native policy and temporary routing before saving the original upstream selection. It leaves native `config.toml` unchanged and closes the adapter when Codex exits. Persistent `cmgr switch`, shared resumption and complete protocol coverage for these transports remain gated.

Synthetic native checks cover exact text, function/custom-tool results and adapter ownership. They do not establish access to a live provider account or every model's dialect. Unsupported request fields and opaque history fail rather than being silently discarded. Messages thinking currently requires an explicit supported budget and native reasoning effort; model-specific adaptive thinking and effort mappings still need validation. A model that requires those newer controls is not covered by the budget-only path.

Messages can deliver an explicitly requested readable summary with `messages_summary = "auto"` in manager settings. Supplied summary text is preserved exactly after complete reply validation; empty or redacted blocks produce no summary. The default is `none`. Concise and detailed summary requests are refused because the adapter does not promise those length controls. Signed replay remains separate from display, and modern prefix preservation still needs validation. See [thinking display](https://platform.claude.com/docs/en/build-with-claude/thinking) and [configuration](Configuration.md).

Model-list requests can reveal catalog entries without proving inference permission. Verify model IDs, tool support, authentication, context limits, and transport with the selected provider. Manual IDs remain provider-specific.

Older direct Anthropic model versions can be retired while a gateway retains an alias. Check the gateway independently. OpenCode Zen uses different transports for different model families.

See [configuration](Configuration.md), [recovery](Recovery.md), and the provider's current API documentation.

Session conversion handles recognized reasoning in response items, replacement histories, guardian checkpoints and readable inter-agent communication. Encrypted-only compaction/messages/tool results cannot be reconstructed and are rejected. The offline catalog discovers bounded plain and compressed rollout headers. Changing a canonical paginated session to a different Responses connection uses an independent new-UUID copy with complete frozen ancestry, source snapshots and original files preserved. Compressed and archived canonical sources are supported by that copy path. Complete independent-copy bundles can be inspected and recovered with `cmgr history-copies`; restoration of original canonical stores and adapter resumption remain pending. File-only replacement of those stores is refused. Other editors and older Codex clients must be stopped before changing saved history.

Messages replies that report changed or mismatched input history in `input_transformations` stop before continuations or tool completions are published, including a reported fallback during streaming. Null or empty reports are accepted. An absent report does not prove that a provider preserved thinking; modern prefix enforcement and shared adapter resumption remain pending.
