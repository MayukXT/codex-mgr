"""Validated rollout conversion with required snapshots and native writer locks."""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from codex_mgr.core.backup import create_session_backup
from codex_mgr.core.rollouts import metadata_from_record, read_session_metadata
from codex_mgr.core.session_lock import session_write_guard
from codex_mgr.utils.files import FileConflictError, file_digest
from codex_mgr.utils.json_edits import JsonEdits


class BridgeError(ValueError):
    pass


def inspect_session_provider(rollout_path: Path) -> str | None:
    try:
        return read_session_metadata(rollout_path).provider
    except (OSError, ValueError):
        return None


def _sanitize_item(
    editor: JsonEdits, item: object, path: tuple, preserve_reasoning=None, *, reasoning_only=False
) -> int:
    if not isinstance(item, dict) or not isinstance(item.get("type"), str):
        raise BridgeError("History has an invalid response item.")
    kind = item["type"]
    encrypted = item.get("encrypted_content")
    if kind in {"compaction", "compaction_summary", "context_compaction"} and encrypted is not None:
        raise BridgeError(
            "This history contains encrypted compaction. Resume it with its original connection; a cross-provider conversion cannot reconstruct that context."
        )
    if kind == "reasoning":
        if not isinstance(item.get("summary"), list):
            raise BridgeError("History has an invalid reasoning summary.")
        if encrypted is not None and not isinstance(encrypted, str):
            raise BridgeError("History has an invalid encrypted reasoning value.")
        if encrypted is not None and preserve_reasoning is not None and preserve_reasoning(item):
            return 0
        if reasoning_only and encrypted is None:
            return 0
        count = 0
        if encrypted is not None:
            editor.set((*path, "encrypted_content"), None)
            count = 1
        if item.get("id") is not None:
            editor.set((*path, "id"), None)
        return count
    # These structured content blocks carry unreadable provider-owned data.
    # Do not mistake similarly named fields in user/tool JSON strings for them.
    if kind in {"agent_message", "function_call_output", "custom_tool_call_output"}:
        content = item.get("content", item.get("output"))
        blocks = content if isinstance(content, list) else []
        if any(
            isinstance(block, dict) and block.get("type") == "encrypted_content" for block in blocks
        ):
            raise BridgeError(
                "This history contains an encrypted message or tool result without a portable plaintext replacement."
            )
    return 0


def _sanitize_record(editor: JsonEdits, preserve_reasoning=None, *, reasoning_only=False) -> int:
    record = editor.value
    payload = record.get("payload")
    kind = record.get("type")
    if kind == "response_item":
        return _sanitize_item(
            editor, payload, ("payload",), preserve_reasoning, reasoning_only=reasoning_only
        )
    if kind == "inter_agent_communication":
        if not isinstance(payload, dict):
            raise BridgeError("History has an invalid inter-agent record.")
        if payload.get("encrypted_content"):
            if reasoning_only:
                raise BridgeError(
                    "This destination check cannot authenticate encrypted inter-agent communication."
                )
            if not isinstance(payload.get("content"), str) or not payload["content"]:
                raise BridgeError(
                    "This history contains encrypted inter-agent communication without readable content."
                )
            editor.set(("payload", "encrypted_content"), None)
            if payload.get("id") is not None:
                editor.set(("payload", "id"), None)
            return 1
    if kind == "compacted":
        if not isinstance(payload, dict) or not isinstance(payload.get("message"), str):
            raise BridgeError("History has an invalid compaction checkpoint.")
        count = 0
        history = payload.get("replacement_history")
        metadata = payload.get("replacement_history_metadata")
        if metadata is not None and (
            not isinstance(metadata, list)
            or not isinstance(history, list)
            or len(metadata) != len(history)
        ):
            raise BridgeError("Compaction history and metadata have different lengths.")
        for key in ("replacement_history", "guardian_history"):
            items = payload.get(key)
            if items is None:
                continue
            if not isinstance(items, list):
                raise BridgeError("History has an invalid nested checkpoint.")
            for index, item in enumerate(items):
                count += _sanitize_item(
                    editor,
                    item,
                    ("payload", key, index),
                    preserve_reasoning,
                    reasoning_only=reasoning_only,
                )
        return count
    return 0


@dataclass
class PreparedBridge:
    rollout: Path
    temporary: Path
    session_id: str
    expected_digest: str
    changed_records: int
    sanitized_count: int
    backup_path: Path | None = None

    def snapshot(self) -> Path | None:
        """Establish the recovery copy before changing selected configuration."""
        if file_digest(self.rollout) != self.expected_digest:
            raise FileConflictError(
                "Session changed during preparation; its current contents were preserved."
            )
        if not self.changed_records:
            return None
        if self.backup_path is None:
            self.backup_path = create_session_backup(self.session_id, self.rollout)
        if (
            file_digest(self.backup_path) != self.expected_digest
            or file_digest(self.rollout) != self.expected_digest
        ):
            raise FileConflictError("Session changed while backing up; conversion was not applied.")
        return self.backup_path

    def commit(self) -> Path | None:
        """Called while the preparation context still owns writer coordination."""
        self.snapshot()
        if self.changed_records:
            os.replace(self.temporary, self.rollout)
        return self.backup_path


@contextmanager
def prepare_bridge(
    rollout_path: Path,
    target_provider_id: str,
    *,
    strip_reasoning: bool = False,
    preserve_reasoning: Callable[[dict], bool] | None = None,
) -> Iterator[PreparedBridge]:
    """Validate the entire input and stage edits before activation or replacement.

    Unknown JSON records stay byte-identical. Only selected scalar values in
    recognized records change; whitespace, decimals, prompts and ordinal counts
    are preserved. A malformed/truncated record aborts the complete operation.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", target_provider_id):
        raise BridgeError("Choose a valid Codex connection identifier.")
    rollout = rollout_path.resolve()
    meta = read_session_metadata(rollout)
    if meta.compressed:
        raise BridgeError(
            "This session is compressed. File conversion requires its canonical store workflow."
        )
    with session_write_guard(meta.id, rollout):
        current = read_session_metadata(rollout)
        if current != meta:
            raise FileConflictError("Session metadata changed; reload before retrying.")
        fd, name = tempfile.mkstemp(
            prefix=f".{rollout.name}.", suffix=".bridge.tmp", dir=rollout.parent
        )
        temporary = Path(name)
        digest = hashlib.sha256()
        changed = count = 0
        saw_meta = False
        try:
            with os.fdopen(fd, "wb") as target, rollout.open("rb") as source:
                os.chmod(temporary, 0o600)
                for number, raw in enumerate(source, 1):
                    digest.update(raw)
                    if not raw.strip():
                        target.write(raw)
                        continue
                    try:
                        editor = JsonEdits(raw.decode("utf-8"))
                    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
                        raise BridgeError(
                            f"Session record {number} is malformed or truncated. Repair the source before converting it."
                        ) from exc
                    if not isinstance(editor.value, dict) or not isinstance(
                        editor.value.get("type"), str
                    ):
                        raise BridgeError(f"Session record {number} is not a rollout object.")
                    if not saw_meta:
                        details = metadata_from_record(editor.value, rollout)
                        if details != meta:
                            raise FileConflictError("Session identity changed during preparation.")
                        if (
                            details.provider != target_provider_id
                            or strip_reasoning
                            or preserve_reasoning is not None
                            or details.from_filename
                        ) and details.canonical_history:
                            raise BridgeError(
                                "This session uses a paginated or inherited history store. File conversion requires its canonical store workflow."
                            )
                        if details.from_filename:
                            editor.set(("payload", "id"), details.id)
                        editor.set(("payload", "model_provider"), target_provider_id)
                        saw_meta = True
                    elif editor.value["type"] == "session_meta":
                        raise BridgeError("Multiple session metadata records are ambiguous.")
                    elif (
                        strip_reasoning
                        or meta.provider != target_provider_id
                        or preserve_reasoning is not None
                    ):
                        count += _sanitize_record(
                            editor,
                            None if strip_reasoning else preserve_reasoning,
                            reasoning_only=not strip_reasoning
                            and meta.provider == target_provider_id,
                        )
                    if editor.edits:
                        changed += 1
                        target.write(editor.render().encode("utf-8"))
                    else:
                        target.write(raw)
                target.flush()
                os.fsync(target.fileno())
            if not saw_meta:
                raise BridgeError("Session metadata is missing.")
            prepared = PreparedBridge(
                rollout, temporary, meta.id, digest.hexdigest(), changed, count
            )
            yield prepared
        finally:
            temporary.unlink(missing_ok=True)


def bridge_session_rollout(
    rollout_path: Path, target_provider_id: str, create_backup_snapshot: bool = True
) -> tuple[bool, int, str]:
    if not create_backup_snapshot:
        return False, 0, "Conversion requires a verified backup; it cannot be disabled."
    try:
        with prepare_bridge(rollout_path, target_provider_id) as prepared:
            prepared.commit()
            return (
                True,
                prepared.sanitized_count,
                "Session connection updated with a recovery snapshot."
                if prepared.changed_records
                else "Session already uses this connection.",
            )
    except (BridgeError, FileConflictError) as exc:
        return False, 0, str(exc)
    except (OSError, ValueError):
        return (
            False,
            0,
            "Session conversion failed. Check permissions and backup storage; the source was preserved.",
        )
