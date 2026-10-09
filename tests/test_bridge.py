"""Conversion preserves unedited bytes and refuses unsafe input or writers."""

import json
from unittest.mock import patch

import pytest

from codex_mgr.core.backup import list_session_backups
from codex_mgr.core.bridge import bridge_session_rollout, inspect_session_provider, prepare_bridge
from codex_mgr.core.session_lock import _native_lock
from codex_mgr.utils.files import FileConflictError, read_snapshot
from codex_mgr.utils.json_edits import JsonEdits
from codex_mgr.utils.paths import get_codex_home

SID = "00000000-0000-0000-0000-000000000001"


def session(tmp_path, *records, provider="openai", metadata=None):
    path = tmp_path / f"rollout-2026-10-10T12-00-00-{SID}.jsonl"
    payload = {"id": SID, "model_provider": provider}
    payload.update(metadata or {})
    path.write_bytes(
        json.dumps({"type": "session_meta", "payload": payload}).encode()
        + b"\r\n"
        + b"".join(json.dumps(record).encode() + b"\n" for record in records)
    )
    return path


def reasoning():
    return {
        "type": "reasoning",
        "id": "rs_synthetic",
        "summary": [{"type": "summary_text", "text": "Visible summary"}],
        "encrypted_content": "opaque-synthetic-ciphertext",
    }


def read_records(path):
    return [json.loads(line) for line in path.read_bytes().splitlines() if line.strip()]


def test_bridge_backs_up_complete_uuid_and_preserves_nonconverted_bytes(tmp_path):
    path = session(tmp_path, {"type": "response_item", "payload": reasoning()})
    unchanged = b'\n {"type":"response_item", "payload":{"type":"message","role":"user","content":[{"type":"input_text","text":" exact prompt  "},{"type":"input_image","image_url":"data:image/png;base64,c3ludGhldGlj"}]},"decimal":0.123456789012345678901234567890}\r\n'
    tool = b'{"type":"response_item","payload":{"type":"function_call_output","call_id":"call_1","output":"{\\"encrypted_content\\":\\"user data\\"}"}}'
    path.write_bytes(path.read_bytes() + unchanged + tool)
    before = path.read_bytes()
    ok, count, _ = bridge_session_rollout(path, "fixture")
    assert ok and count == 1
    assert unchanged in path.read_bytes() and path.read_bytes().endswith(tool)
    assert read_snapshot(list_session_backups(SID)[0]) == before
    records = read_records(path)
    assert records[1]["payload"]["encrypted_content"] is None
    assert records[1]["payload"]["id"] is None
    assert records[1]["payload"]["summary"] == reasoning()["summary"]
    assert inspect_session_provider(path) == "fixture"


def test_nested_compaction_reasoning_and_subagent_content_are_scoped(tmp_path):
    nested = {
        "type": "compacted",
        "payload": {
            "message": "Visible checkpoint",
            "replacement_history": [
                reasoning(),
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "keep encrypted_content untouched"}],
                },
            ],
            "replacement_history_metadata": [{"source": "original"}, {}],
            "guardian_history": [reasoning()],
            "retained_context": {"encrypted_content": "application data"},
        },
    }
    communication = {
        "type": "inter_agent_communication",
        "payload": {
            "author": "/root",
            "recipient": "/root/worker",
            "content": "Visible worker response",
            "id": "rs_worker",
            "encrypted_content": "opaque-worker",
            "trigger_turn": False,
        },
    }
    path = session(tmp_path, nested, communication)
    ok, count, _ = bridge_session_rollout(path, "fixture")
    assert ok and count == 3
    payload = read_records(path)[1]["payload"]
    assert payload["replacement_history"][0]["encrypted_content"] is None
    assert payload["guardian_history"][0]["id"] is None
    assert (
        payload["replacement_history_metadata"] == nested["payload"]["replacement_history_metadata"]
    )
    assert payload["retained_context"]["encrypted_content"] == "application data"
    assert read_records(path)[2]["payload"]["content"] == "Visible worker response"


@pytest.mark.parametrize(
    "bad",
    [
        b'{"type":',
        b"[]\n",
        b"not JSON\n",
        b'{"type":"response_item","type":"compacted","payload":{}}\n',
        b'{"type":"unknown","n":NaN}\n',
        b"\xff\n",
    ],
)
def test_malformed_or_truncated_records_abort_before_snapshot(tmp_path, bad):
    path = session(tmp_path)
    path.write_bytes(path.read_bytes() + bad)
    before = path.read_bytes()
    ok, _, _ = bridge_session_rollout(path, "fixture")
    assert not ok
    assert path.read_bytes() == before
    assert not list_session_backups(SID)
    assert not list(tmp_path.glob("*.bridge.tmp"))


@pytest.mark.parametrize(
    "record",
    [
        {
            "type": "response_item",
            "payload": {"type": "compaction", "encrypted_content": "opaque-context"},
        },
        {
            "type": "compacted",
            "payload": {
                "message": "",
                "replacement_history": [
                    {"type": "compaction_summary", "encrypted_content": "opaque-context"}
                ],
            },
        },
        {
            "type": "inter_agent_communication",
            "payload": {"content": "", "encrypted_content": "opaque-worker"},
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": [{"type": "encrypted_content", "encrypted_content": "opaque-tool"}],
            },
        },
        {
            "type": "compacted",
            "payload": {
                "message": "summary",
                "replacement_history": [],
                "replacement_history_metadata": [{}],
            },
        },
    ],
)
def test_unportable_encrypted_context_is_preserved_and_explained(tmp_path, record):
    path = session(tmp_path, record)
    before = path.read_bytes()
    ok, _, message = bridge_session_rollout(path, "fixture")
    assert not ok and message
    assert path.read_bytes() == before
    assert not list_session_backups(SID)


def test_repeat_and_reverse_conversion_are_idempotent(tmp_path):
    path = session(tmp_path, {"type": "response_item", "payload": reasoning()})
    assert bridge_session_rollout(path, "fixture")[0]
    after = path.read_bytes()
    assert bridge_session_rollout(path, "fixture")[:2] == (True, 0)
    assert path.read_bytes() == after and len(list_session_backups(SID)) == 1
    assert bridge_session_rollout(path, "openai")[:2] == (True, 0)
    payload = read_records(path)[1]["payload"]
    assert payload["id"] is None and payload["encrypted_content"] is None
    assert payload["summary"] == reasoning()["summary"]
    assert len(list_session_backups(SID)) == 2


def test_missing_metadata_id_is_recovered_from_full_filename(tmp_path):
    path = session(tmp_path, metadata={"id": None})
    assert bridge_session_rollout(path, "fixture")[0]
    assert read_records(path)[0]["payload"]["id"] == SID


def test_required_snapshot_and_backup_failure_preserve_source(tmp_path):
    path = session(tmp_path)
    before = path.read_bytes()
    assert not bridge_session_rollout(path, "fixture", create_backup_snapshot=False)[0]
    with patch("codex_mgr.core.bridge.create_session_backup", side_effect=OSError):
        assert not bridge_session_rollout(path, "fixture")[0]
    assert path.read_bytes() == before


def test_external_append_during_preparation_is_not_replaced(tmp_path):
    path = session(tmp_path)
    with prepare_bridge(path, "fixture") as prepared:
        path.write_bytes(path.read_bytes() + b'{"type":"unknown","new":"external"}\n')
        with pytest.raises(FileConflictError):
            prepared.commit()
    assert b'"external"' in path.read_bytes()


def test_native_writer_lock_blocks_conversion_without_override(tmp_path):
    path = session(tmp_path)
    folder = get_codex_home() / "thread-writer-locks"
    folder.mkdir(parents=True)
    before = path.read_bytes()
    with _native_lock(folder / f"{SID}.lock", create=True):
        ok, _, message = bridge_session_rollout(path, "fixture")
    assert not ok and "active writer" in message
    assert path.read_bytes() == before and not list_session_backups(SID)


def test_json_scalar_edits_preserve_precision_spacing_and_escapes():
    raw = '{ "payload" : {"encrypted_content":"secret","n":0.12345678901234567890123456789,"text":"\\u0041\\n"}, "huge":123456789012345678901234567890 }\r\n'
    editor = JsonEdits(raw)
    editor.set(("payload", "encrypted_content"), None)
    assert editor.render() == raw.replace('"secret"', "null")
    editor = JsonEdits('{"payload":{}}')
    editor.set(("payload", "id"), SID)
    editor.set(("payload", "model_provider"), "fixture")
    assert json.loads(editor.render())["payload"] == {"id": SID, "model_provider": "fixture"}


def test_paginated_or_inherited_history_only_allows_direct_resume(tmp_path):
    for metadata in [
        {"history_mode": "paginated"},
        {"history_base": {"thread_id": SID, "ordinal": 4}},
    ]:
        path = session(tmp_path, provider="fixture", metadata=metadata)
        before = path.read_bytes()
        assert bridge_session_rollout(path, "fixture")[:2] == (True, 0)
        assert path.read_bytes() == before
        ok, _, message = bridge_session_rollout(path, "other")
        assert not ok and "canonical store" in message
        assert path.read_bytes() == before and not list_session_backups(SID)
