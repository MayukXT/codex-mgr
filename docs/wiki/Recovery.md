# Session recovery

Session conversion changes saved Codex rollout files. Removing encrypted reasoning removes information that cannot be reconstructed from visible conversation text.

The current 1.0.0 backup lookup and restore paths have known issues. Before modifying an important session, stop active Codex writers and make an independent copy of the rollout and configuration. Preserve the complete session UUID.

```sh
cmgr restore <session-uuid>
```

Restore replaces session state. Preserve newer turns before restoring an older snapshot. Backups can contain prompts, source code, tool output, configuration, and credentials; keep them private.

[Compatibility](Compatibility.md) describes the current supported limits.
