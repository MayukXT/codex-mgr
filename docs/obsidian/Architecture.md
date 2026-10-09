# Architecture Overview

The **Codex Manager** (`codex-mgr` / `cmgr`) is an open-source utility designed to eliminate provider lock-in and session resumption barriers in OpenAI Codex CLI.

## Core Pillars
1. **Dynamic Provider Management:** Seamlessly orchestrate providers and models without risking existing configuration. See [[Provider-Compatibility-Matrix]] and [[Model-Discovery-And-Cache-Sync]].
2. **Universal Session Bridging:** Bi-directional reasoning trace sanitization that allows resuming any session across any provider. See [[Reasoning-Blobs-And-Fernet]].
3. **Anthropic 5.5 Protocol Adaptation:** Local loopback micro-proxy enabling official Claude endpoints to speak Codex's Responses wire protocol. See [[Anthropic-5.5-Proxy-Adapter]].
4. **Zero-Loss Disaster Recovery:** Atomic snapshots and instant rollback. See [[Disaster-Recovery-And-Backups]].
5. **Strict Public Safety & Privacy:** Guaranteed zero credential leakage. See [[Security-And-Privacy-Guide]].

## High-Level Component Flow
```
[User CLI / TUI]
       │
       ▼
[CLI Dispatcher / TUI Dashboard]
       │
       ├─────────────────────────────────┬───────────────────────────────┐
       ▼                                 ▼                               ▼
[Provider Registry]              [Session Resolver]              [Config Manager]
  - Presets catalog                - Scans session_index.jsonl     - Surgical TOML patch
  - Probes /v1/models              - Identifies origin provider    - Preserves MCP & hooks
       │                                 │                               │
       │                                 ▼                               ▼
       │                     [Session Bridge Engine]             [config.toml]
       │                       - Strips Fernet blobs                     │
       │                       - Updates session_meta                    │
       │                       - Atomic file swap                        │
       │                                 │                               │
       └─────────────────────────────────┼───────────────────────────────┘
                                         ▼
                               [Codex Process Runner]
                                 - Interactive handoff
                                 - (Optional) Loopback proxy
```

## Related Links
- [[Reasoning-Blobs-And-Fernet]]
- [[Provider-Compatibility-Matrix]]
- [[Anthropic-5.5-Proxy-Adapter]]
- [[Model-Discovery-And-Cache-Sync]]
- [[Disaster-Recovery-And-Backups]]
- [[Security-And-Privacy-Guide]]
