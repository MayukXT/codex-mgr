"""Universal cross-provider session bridge engine.

Sanitizes proprietary encrypted reasoning blobs (Fernet tokens) and aligns
session metadata across providers without losing prompt tokens, code diffs,
or execution outputs.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from codex_mgr.core.backup import create_session_backup


def inspect_session_provider(rollout_path: Path) -> Optional[str]:
    """Inspect the bound provider in Line 0 (session_meta) of a session rollout."""
    if not rollout_path.exists():
        return None

    try:
        with open(rollout_path, "r", encoding="utf-8") as f:
            first_line = f.readline()
            if not first_line:
                return None
            data = json.loads(first_line)
            if data.get("type") == "session_meta":
                return data.get("payload", {}).get("model_provider", "openai")
    except Exception:
        return None
    return "openai"


def bridge_session_rollout(
    rollout_path: Path,
    target_provider_id: str,
    create_backup_snapshot: bool = True
) -> Tuple[bool, int, str]:
    """Bridge a session rollout file to a target provider environment.

    Returns:
        (success, sanitized_blob_count, message)
    """
    if not rollout_path.exists():
        return False, 0, f"Rollout file not found: {rollout_path}"

    origin_provider = inspect_session_provider(rollout_path) or "openai"

    # Extract session UUID from filename or metadata
    session_id = rollout_path.stem.split("-")[-1]

    # Create atomic snapshot before touching anything
    if create_backup_snapshot:
        try:
            create_session_backup(session_id, rollout_path)
        except Exception as e:
            return False, 0, f"Failed to create pre-bridge backup: {str(e)}"

    tmp_path = rollout_path.with_suffix(".bridge.tmp")
    sanitized_count = 0

    try:
        with open(rollout_path, "r", encoding="utf-8") as src, \
             open(tmp_path, "w", encoding="utf-8", newline="\n") as dst:

            for line_idx, line in enumerate(src):
                line_str = line.strip()
                if not line_str:
                    continue

                try:
                    record = json.loads(line_str)
                except Exception:
                    # If an existing line is invalid, write unchanged to avoid losing raw data
                    dst.write(line_str + "\n")
                    continue

                rec_type = record.get("type")

                # 1. Update session_meta (Line 0)
                if rec_type == "session_meta":
                    payload = record.get("payload", {})
                    payload["model_provider"] = target_provider_id
                    record["payload"] = payload
                    dst.write(json.dumps(record, ensure_ascii=False) + "\n")
                    continue

                # 2. Bridge reasoning items
                if rec_type == "response_item":
                    payload = record.get("payload", {})
                    item_type = payload.get("type")

                    if item_type == "reasoning":
                        # If moving away from OpenAI, strip proprietary encrypted_content
                        if target_provider_id != "openai":
                            if "encrypted_content" in payload and payload["encrypted_content"]:
                                payload["encrypted_content"] = None
                                sanitized_count += 1
                        else:
                            # Moving to OpenAI: ensure reasoning block has standard structure
                            if payload.get("encrypted_content") is None and payload.get("content") is None:
                                # Synthetic placeholder: retain empty summary to prevent reducer panic
                                payload["summary"] = payload.get("summary", [])

                        record["payload"] = payload
                    dst.write(json.dumps(record, ensure_ascii=False) + "\n")
                    continue

                # 3. Subagent communication frames
                if "encrypted_content" in line_str:
                    payload = record.get("payload", {})
                    if isinstance(payload, dict) and "encrypted_content" in payload:
                        payload["encrypted_content"] = None
                        sanitized_count += 1
                        record["payload"] = payload

                dst.write(json.dumps(record, ensure_ascii=False) + "\n")

        # Atomic replacement: only replace original file if entire write succeeded
        tmp_path.replace(rollout_path)
        return True, sanitized_count, (
            f"Successfully bridged session from '{origin_provider}' to '{target_provider_id}' "
            f"({sanitized_count} reasoning blobs sanitized)."
        )

    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        return False, 0, f"Bridge conversion aborted due to error: {str(exc)}"
