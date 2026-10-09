# Reasoning Blobs & Fernet Cryptography in Codex

## Background
In OpenAI's Responses API (`wire_api = "responses"`), frontier reasoning models (`gpt-6.1-sol`, `gpt-6-astra`, `gpt-5.6-terra`) output proprietary reasoning traces in a dedicated item block:
```json
{
  "type": "response_item",
  "payload": {
    "type": "reasoning",
    "id": "rs_0153d758de40eda001...",
    "summary": [],
    "content": null,
    "encrypted_content": "gAAAAABqx7Y..."
  }
}
```

## The Mechanism
1. **Fernet Symmetric Encryption:** The `encrypted_content` string is a standard Fernet ciphertext generated server-side by OpenAI with proprietary rotation keys.
2. **Stateless Turn Continuity:** OpenAI uses these encrypted blobs to resume the model's chain-of-thought internal state across turns without exposing the raw internal thinking tokens to client applications.
3. **Subagent Frame Serialization:** Subagent message submissions (`InterAgentCommunication`) also encapsulate these blobs to pass reasoning between child workers.

## Why Cross-Provider Resumption Fails
- **OpenAI $\to$ Router/Third-Party:** Upstream gateways (AgentRouter, OpenRouter, DeepSeek, Local Ollama) validate request schemas against public specs. When they encounter OpenAI's proprietary `encrypted_content` property, they reject the payload with `HTTP 400 Bad Request: unknown field 'encrypted_content'`.
- **Router/Third-Party $\to$ OpenAI:** When a session modified on a router is switched back to OpenAI, synthetic reasoning objects lacking valid OpenAI HMAC/Fernet signatures fail server validation, and Codex's internal Rust state reducer reports identity mismatch collisions:
  `reasoning item merge attempted with different encrypted_content identity`

## The Solution: Streaming Sanitizer
The `codex-mgr` bridge engine (`core/bridge.py`):
1. Creates an atomic backup in `~/.codex/backups/sessions/` before modifying anything.
2. Stream-parses the rollout `.jsonl` file.
3. Purges `encrypted_content` ciphertext properties from all reasoning payloads and inter-agent frames.
4. Preserves 100% of user prompt tokens, assistant textual outputs, tool invocations, and command outputs.
5. Re-binds `session_meta.payload.model_provider` to match the target environment.

## Related Links
- [[Architecture]]
- [[Provider-Compatibility-Matrix]]
- [[Disaster-Recovery-And-Backups]]
