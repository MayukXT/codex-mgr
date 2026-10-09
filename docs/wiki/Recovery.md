# Session recovery

## Find a session

```sh
cmgr sessions
cmgr sessions --search "title or UUID prefix"
cmgr sessions --workspace . --include-archived
cmgr sessions --limit 20 --offset 20 --json
```

The local catalog reads session titles from `session_index.jsonl` and complete UUIDs from rollout metadata. It also discovers unindexed JSONL rollouts. Archived files are included when requested. Workspace filters match the original working directory exactly.

There is no fixed history cutoff. Missing files and duplicate UUID files are identified in the listing. Resolve duplicate files before resuming or restoring; a title or prefix that matches several sessions requires a complete UUID. Compressed rollouts and hosted sessions are outside this offline catalog.

These commands print local session metadata. JSON output includes titles, UUIDs, timestamps and absolute working-directory paths. Use filters or page bounds for a narrower result and review the output before sharing it.

## Convert and restore

Session conversion changes saved Codex rollout files. Removing encrypted reasoning removes information that cannot be reconstructed from visible conversation text.

The current 1.0.0 backup lookup and restore paths have known issues. Before modifying an important session, stop active Codex writers and make an independent copy of the rollout and configuration. Preserve the complete session UUID.

```sh
cmgr restore <session-uuid>
```

Restore replaces session state. Preserve newer turns before restoring an older snapshot. Backups can contain prompts, source code, tool output, configuration, and credentials; keep them private.

[Compatibility](Compatibility.md) describes the current supported limits.
