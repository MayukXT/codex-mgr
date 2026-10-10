# Recovery

## Configuration changes

`cmgr recover` restores matching files from an interrupted activation or completes a native-validated change when both files match. An external edit stops automatic recovery. Review pending transaction IDs and file states with:

```sh
cmgr recover --list
cmgr recover --accept-external TRANSACTION_ID
```

The explicit second command keeps the current configuration and manager files. It saves checksummed snapshots of both existing files before settling that one transaction. If a file changed after review or snapshot creation failed, the marker stays pending. Keeping files does not validate their configuration; reload settings and run diagnostics before activating a connection. The terminal menu exposes these choices under Tools > Activation recovery, with confirmation defaulting to No.

Recovery reports transaction IDs and whether a file is original, requested, unchanged or externally edited. It does not print the file contents or credentials. Review snapshots before sharing them because configuration and manager snapshots may contain plaintext credentials.

## Configuration revisions

```sh
cmgr revisions list
cmgr revisions list --kind config
cmgr revisions restore --config LISTED_FILENAME
cmgr revisions restore --settings LISTED_FILENAME
cmgr revisions restore --config CONFIG_FILENAME --settings SETTINGS_FILENAME --repair
```

Choose exact filenames from the listing. Only checksummed configuration and manager revisions in the selected Codex home's backup directories are supported. Linked files/directories, arbitrary paths, missing checksums and corrupt or oversized revisions are rejected. Listing shows metadata and corruption status, without printing file contents. A verified checksum does not establish that Codex will accept the configuration.

A config revision restores its exact bytes, including comments and unrelated tables, and reconciles the manager's selected connection and model. A settings revision restores manager preferences and connections, then applies its selected connection to current TOML while preserving unrelated settings. When both are selected, the config revision determines the connection and model. Manager JSON is validated and serialized when saved.

Current files are snapshotted before replacement. Changes after review stop restoration. Native configuration layers and managed requirements are checked before and after the write; failed verification restores matching original files. An external edit remains pending for explicit recovery. Interrupted restoration uses the same `cmgr recover` workflow as connection activation.

Malformed current files require explicit `--repair` and a matching valid revision for each malformed file. A config-only restore cannot discard unreadable manager state, and a settings-only restore cannot replace unreadable TOML with an empty file. Repair preserves malformed originals in snapshots. Explicit repair also supports current TOML that parses but Codex cannot load. Preflight is attempted whenever the current TOML parses; a native read failure can proceed to repair, while a reported policy or layer conflict still stops before writes. Post-write native verification remains mandatory before committing manager state. Legacy profile revisions require explicit migration before restoration. Custom definitions using reserved built-in IDs must be explicitly renamed along with their `model_provider` selection; restoring an invalid definition does not rename it automatically.

The terminal menu exposes config, settings and paired restores under Tools > Configuration revisions. Repair current files selects a pair of revisions with explicit repair permission. Selected complete filenames appear before confirmation, which defaults to No. If settings cannot be loaded, the entry screen offers recovery tools. The repair choice and final restore confirmation are separate.

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

Resume validates the selected connection, exact model and required credentials before changing a session. It uses the Codex connection ID from configuration, which can differ from the manager's friendly connection ID. Invalid or truncated JSONL stops conversion before any session replacement.

```sh
cmgr resume <session-uuid> --to <connection-id>
cmgr resume <session-uuid> --to <connection-id> --allow-reasoning-loss
```

The second form explicitly allows removal of encrypted reasoning. Readable summaries remain. The menu previews the conversion and asks before removing encrypted items. Encrypted-only compaction, messages and tool results have no portable plaintext replacement, so conversion stops and preserves the source. Paginated or inherited history stores require Codex's canonical store workflow for conversion; direct resume with the original connection remains available.

Conversion preserves record order and every source byte outside the selected metadata/reasoning values. Unknown records, images, function calls and tool output are retained. This establishes file preservation, not successful inference on every endpoint.

Codex 0.159.0 writer locks coordinate conversion and restore with the selected session's native writer. An active writer blocks the operation. Older Codex versions and other editors must be stopped before editing saved history because they do not share this protocol. Expected-content checks also stop replacement when a file changed during preparation.

```sh
cmgr restore <session-uuid>
cmgr restore <session-uuid> --list
cmgr restore <session-uuid> --snapshot <listed-filename>
```

Restore verifies snapshot integrity and identity, then saves current content in a new snapshot before replacement. Legacy backups stored under a 12-character UUID suffix are listed only when their metadata matches the complete UUID. Restoring a legacy copy also creates a checksummed copy in the full UUID directory and retains the legacy original. Corrupt, truncated or mismatched snapshots are rejected.

Backups can contain prompts, source code, tool output, configuration, and credentials; keep them private. Checksums detect corruption and do not authenticate a snapshot against deliberate replacement of both the file and checksum.

[Compatibility](Compatibility.md) describes the current supported limits.
