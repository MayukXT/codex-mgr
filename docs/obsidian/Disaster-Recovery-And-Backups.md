# Disaster Recovery & Atomic Backups

## Principles
1. **Never mutate without snapshotting:** Every modified file is backed up before any write operation.
2. **Transactional Writes:** File operations write to `.tmp` files first, validate syntax, and perform atomic file replacement (`os.replace`).
3. **Instant Rollback:** Any bridged session can be reverted to its exact original state with a single command.

## Backup Layout
Backups are organized in `~/.codex/backups/`:
```
~/.codex/backups/
├── sessions/
│   └── <session_uuid>/
│       ├── 2026-10-09T18-00-00.rollout.jsonl
│       └── 2026-10-09T18-15-20.rollout.jsonl
└── settings/
    └── cmgr.json.bak.2026-10-09T18-00-00
```

## Rollback Command
Users can restore a session either via the interactive TUI (**"Undo / Revert a Session"**) or via CLI:
```bash
cmgr restore <session_uuid>
```

## Related Links
- [[Architecture]]
- [[Reasoning-Blobs-And-Fernet]]
- [[Security-And-Privacy-Guide]]
