# Model Discovery & Cache Synchronization

## Overview
`codex-mgr` implements a dual-mode model intelligence system:
1. **Dynamic Remote Probing (`GET /v1/models`):** Queries remote providers at zero token cost.
2. **Local Cache Ingestion (`~/.codex/models_cache.json`):** Automatically syncs with Codex's local model cache.

## Local Cache Ingestion
Codex stores server-pushed model definitions in `~/.codex/models_cache.json`:
- `gpt-6.1-sol`
- `gpt-6-astra`
- `gpt-5.6-terra`
- Reasoning levels: `['low', 'medium', 'high', 'xhigh', 'max', 'ultra']`
- Service tier definitions (`priority` / `Fast`)

When `cmgr` launches, it reads this cache file. Any model officially added by OpenAI is immediately available in the TUI without requiring a software update.

## Zero-Token Probing
When users configure a remote provider (e.g., OpenRouter or a private gateway):
1. `cmgr` issues a lightweight HTTP `GET /v1/models`.
2. Consumes **0 prompt tokens**.
3. Parses returned model IDs and sorts them by recency and family.
4. Falls back to manual entry with clear prompts if the endpoint has models listing disabled.

## Related Links
- [[Architecture]]
- [[Provider-Compatibility-Matrix]]
- [[Disaster-Recovery-And-Backups]]
