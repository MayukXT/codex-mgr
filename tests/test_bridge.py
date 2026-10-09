"""Unit tests for the universal cross-provider session bridge."""

import json
from pathlib import Path

from codex_mgr.core.bridge import bridge_session_rollout, inspect_session_provider


def test_bridge_session_strips_encrypted_content(tmp_path: Path):
    # 1. Create a synthetic rollout file with simulated encrypted reasoning blobs
    rollout_file = tmp_path / "rollout-mock-0000.jsonl"

    lines = [
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "mock-0000", "model_provider": "openai"}
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": "Hello, world!"
            }
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "id": "rs_mock_12345",
                "summary": [],
                "content": None,
                "encrypted_content": "gAAAAABmockCiphertextBlob123456789=="
            }
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": "I am here to help!"
            }
        }),
    ]

    rollout_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    assert inspect_session_provider(rollout_file) == "openai"

    # 2. Bridge to AgentRouter
    ok, count, msg = bridge_session_rollout(
        rollout_file,
        target_provider_id="agentrouter",
        create_backup_snapshot=False
    )

    assert ok is True
    assert count == 1

    # 3. Verify content
    updated_lines = [json.loads(l) for l in rollout_file.read_text(encoding="utf-8").splitlines() if l.strip()]

    # Verify session_meta updated
    assert updated_lines[0]["payload"]["model_provider"] == "agentrouter"

    # Verify user message intact
    assert updated_lines[1]["payload"]["content"] == "Hello, world!"

    # Verify encrypted_content was cleanly stripped (None)
    assert updated_lines[2]["payload"]["encrypted_content"] is None

    # Verify assistant message intact
    assert updated_lines[3]["payload"]["content"] == "I am here to help!"


def test_bridge_back_to_openai(tmp_path: Path):
    rollout_file = tmp_path / "rollout-mock-router.jsonl"

    lines = [
        json.dumps({
            "type": "session_meta",
            "payload": {"id": "mock-router", "model_provider": "agentrouter"}
        }),
        json.dumps({
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": "Testing reverse bridge"
            }
        })
    ]
    rollout_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    ok, count, msg = bridge_session_rollout(
        rollout_file,
        target_provider_id="openai",
        create_backup_snapshot=False
    )

    assert ok is True
    assert inspect_session_provider(rollout_file) == "openai"
