# Anthropic 5.5 Protocol Proxy Adapter

## Problem Statement
Official Anthropic endpoints (`api.anthropic.com`) exclusively accept the Anthropic Messages protocol (`/v1/messages`), utilizing Anthropic-specific authentication headers (`x-api-key`, `anthropic-version`). Codex CLI, however, communicates exclusively via OpenAI Responses wire format or Chat Completions.

## Architectural Solution
`codex-mgr` includes a lightweight, built-in loopback proxy (`core/proxy.py`):
1. **Loopback Binding:** Listens on `127.0.0.1:4099` (or dynamically selected port).
2. **Protocol Translation:**
   - Translates incoming OpenAI-style requests into Anthropic Messages payloads.
   - Converts system instructions, conversation turns, and tool calls.
   - Maps Codex's `model_reasoning_effort` to Anthropic's adaptive thinking budgets:
     - `low` $\to$ 4,096 tokens
     - `medium` $\to$ 8,192 tokens
     - `high` $\to$ 16,384 tokens
     - `xhigh` $\to$ 32,768 tokens
     - `max` $\to$ 65,536 tokens
     - `ultra` $\to$ 131,072 tokens
3. **Server-Sent Events (SSE) Streaming:** Translates Anthropic's event stream (`content_block_delta`, `thinking_delta`) into OpenAI SSE chunks with zero latency overhead.
4. **Transparent Routing:** Codex's `config.toml` is configured to point its provider base URL to `http://127.0.0.1:4099/v1` whenever the Anthropic profile is active.

## Related Links
- [[Architecture]]
- [[Provider-Compatibility-Matrix]]
- [[Security-And-Privacy-Guide]]
