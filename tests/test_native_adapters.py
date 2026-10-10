"""Opt-in native Codex requests against synthetic loopback protocol providers."""

import json
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import tomlkit
import zstandard
from test_adapter_runtime import synthetic_loopback_http, upstream  # noqa: F401 -- shared fixture
from test_streaming_codecs import block, chat, delta, finish, start, stop, wire

from codex_mgr.config.schema import ModelInfo, ProviderConfig
from codex_mgr.core.adapters.launch import AdapterLaunch, adapter_connection_id
from codex_mgr.core.adapters.runtime import AdapterRuntime
from codex_mgr.core.backup import (
    create_session_backup,
    list_session_backups,
    restore_session_backup,
)
from codex_mgr.core.bridge import BridgeError, prepare_bridge
from codex_mgr.core.catalog import catalog_document
from codex_mgr.core.history_copy import (
    prepare_history_copy,
    read_history_copy,
    recover_history_copy,
)
from codex_mgr.core.history_lineage import resolve_history_lineage
from codex_mgr.core.history_stream import iter_history_records
from codex_mgr.core.rollouts import read_session_metadata
from codex_mgr.core.rpc import CodexReader
from codex_mgr.core.session_lock import session_write_guards
from codex_mgr.core.sessions import find_session_by_id_or_title
from codex_mgr.utils.files import FileConflictError, create_snapshot
from codex_mgr.utils.json_edits import JsonEdits
from codex_mgr.utils.paths import find_codex_binary, get_backups_dir, get_codex_home
from codex_mgr.utils.processes import OwnedBackgroundProcess

pytestmark = pytest.mark.skipif(
    os.environ.get("CMGR_NATIVE_CONTRACTS") != "1",
    reason="Set CMGR_NATIVE_CONTRACTS=1 with Codex 0.159.0 installed.",
)


def test_native_canonical_history_rejects_file_only_mutations(tmp_path, monkeypatch):
    from test_continuations import thinking_stream

    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream("messages", responder=lambda _: wire(thinking_stream(), "messages")[0]) as (
        provider,
        _,
        _,
    ):
        provider.reasoning_budget = 1024
        with AdapterRuntime(provider) as runtime:
            first = launch(tmp_path, runtime, reasoning=True)
            identity = first[0]["thread_id"]
            rollout = next((get_codex_home() / "sessions").rglob("*.jsonl"))
            snapshot = create_snapshot(
                rollout,
                get_backups_dir() / "sessions" / identity,
                suffix=".rollout.jsonl",
            )
            compressed = rollout.with_suffix(".jsonl.zst")
            raw_rollout = rollout.read_bytes()
            compressed.write_bytes(
                zstandard.ZstdCompressor(write_checksum=True).compress(raw_rollout)
            )
            rollout.unlink()
            original_compressed = compressed.read_bytes()
            assert list(iter_history_records(compressed)) == raw_rollout.splitlines(keepends=True)
            discovered = find_session_by_id_or_title(identity)
            assert discovered.require_rollout() == compressed
            assert compressed.read_bytes() == original_compressed and not rollout.exists()
            launch(tmp_path, runtime, session=identity, reasoning=True)
            assert rollout.exists() and not compressed.exists()
            before_restore = rollout.read_bytes()
            with pytest.raises(ValueError, match="canonical store"):
                create_session_backup(identity, rollout)
            with pytest.raises(ValueError, match="canonical store"):
                restore_session_backup(identity, rollout, snapshot)
            assert rollout.read_bytes() == before_restore
            assert list_session_backups(identity) == [snapshot]
            resumed = launch(tmp_path, runtime, session=identity, reasoning=True)
            assert resumed[-1]["type"] == "turn.completed"
    original = rollout.read_bytes()
    with upstream("chat") as (provider, received, _), AdapterRuntime(provider) as runtime:
        with (
            pytest.raises(BridgeError, match="canonical"),
            prepare_bridge(rollout, "fixture", preserve_reasoning=runtime.reasoning_compatible),
        ):
            pytest.fail("Canonical native history cannot be converted by replacing its file.")
        assert rollout.read_bytes() == original and list_session_backups(identity) == [snapshot]
        assert not received


def test_native_revert_keeps_thread_identity_when_rollout_identity_changes(tmp_path, monkeypatch):
    class SyntheticWriter(CodexReader):
        ALLOWED = CodexReader.ALLOWED | {"thread/resume", "thread/revert"}

    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream() as (provider, _, _), AdapterRuntime(provider) as runtime:
        identity = launch(tmp_path, runtime)[0]["thread_id"]
        launch(tmp_path, runtime, session=identity)
        source = find_session_by_id_or_title(identity).require_rollout()
        records = [json.loads(raw) for raw in source.read_bytes().splitlines()]
        turn_id = next(
            record["payload"]["turn_id"]
            for record in reversed(records)
            if record["type"] == "turn_context"
        )
        environment = os.environ.copy()
        environment["CMGR_NATIVE_FIXTURE_KEY"] = runtime.token
        with SyntheticWriter(environment=environment) as writer:
            writer.request("thread/resume", {"threadId": identity})
            reverted = writer.request(
                "thread/revert", {"threadId": identity, "beforeTurnId": turn_id}
            )["thread"]
        replacement = Path(reverted["path"])
        assert reverted["id"] == identity and replacement != source
        metadata = read_session_metadata(replacement)
        assert metadata.id == identity and metadata.canonical_history
        assert metadata.rollout_id != identity
        assert source.exists()
        assert find_session_by_id_or_title(identity).require_rollout() == replacement
        assert launch(tmp_path, runtime, session=identity)[-1]["type"] == "turn.completed"


def test_native_independent_materialized_history_preserves_source_and_resumes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream() as (provider, received, _), AdapterRuntime(provider) as runtime:
        identity = launch(tmp_path, runtime)[0]["thread_id"]
        source = find_session_by_id_or_title(identity).require_rollout()
        original = source.read_bytes()
        raw_meta, suffix = original.split(b"\n", 1)
        editor = JsonEdits(raw_meta.decode())
        imported_id = str(uuid4())
        editor.set(("payload", "id"), imported_id)
        editor.set(("payload", "history_mode"), "legacy")
        editor.set(("payload", "history_base"), None)
        editor.set(("payload", "forked_from_id"), identity)
        imported = source.with_name(source.name.replace(identity, imported_id))
        imported.write_bytes(editor.render().encode() + b"\n" + suffix)
        resumed = launch(tmp_path, runtime, session=imported_id)
        assert resumed[0]["thread_id"] == imported_id
        assert source.read_bytes() == original
        assert imported.read_bytes().startswith(editor.render().encode() + b"\n" + suffix)
        assert len(received) == 2
        assert any(
            message["role"] == "assistant"
            and message["content"] == [{"type": "text", "text": "  Exact 🌙\n"}]
            for message in received[-1][2]["messages"]
        )


def test_native_canonical_cross_protocol_copy_keeps_readable_context_and_originals(
    tmp_path, monkeypatch
):
    from test_continuations import thinking_stream

    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream("messages", responder=lambda _: wire(thinking_stream(), "messages")[0]) as (
        provider,
        _,
        _,
    ):
        provider.reasoning_budget = 1024
        with AdapterRuntime(provider) as runtime:
            original_id = launch(tmp_path, runtime, reasoning=True)[0]["thread_id"]
            source = find_session_by_id_or_title(original_id).require_rollout()
            # The signed fixture has no public summary. Add an explicit synthetic
            # readable summary to exercise native replay without decrypting thinking.
            records = source.read_bytes().splitlines(keepends=True)
            summary = "  Exact readable summary 🌙\n"
            for index, raw in enumerate(records):
                editor = JsonEdits(raw.decode())
                if (
                    editor.value["type"] == "response_item"
                    and editor.value["payload"].get("type") == "reasoning"
                ):
                    editor.set(("payload", "summary"), [{"type": "summary_text", "text": summary}])
                    records[index] = editor.render().encode()
                    break
            else:
                pytest.fail("Synthetic native reasoning item was not persisted.")
            source.write_bytes(b"".join(records))
            original = source.read_bytes()
    with upstream("chat") as (provider, received, _), AdapterRuntime(provider) as runtime:
        with prepare_history_copy(
            source, "fixture", preserve_reasoning=runtime.reasoning_compatible
        ) as prepared:
            assert prepared.sanitized_count == 2
            with pytest.raises(BridgeError, match="consent"):
                prepared.publish()
            imported_id = prepared.id
            prepared.publish(allow_reasoning_loss=True)
        assert source.read_bytes() == original
        resumed = launch(tmp_path, runtime, session=imported_id)
        assert resumed[0]["thread_id"] == imported_id
        assert source.read_bytes() == original
        prior = [item for item in received[-1][2]["messages"] if item["role"] == "assistant"]
        texts = [
            part["text"]
            for message in prior
            for part in message["content"]
            if part["type"] == "text"
        ]
        assert summary in texts and "Exact response." in texts
        info = read_history_copy(imported_id)
        assert info.original_id == original_id and info.sanitized_count == 2
        assert recover_history_copy(imported_id).state == "published"


@pytest.mark.parametrize(
    "representation", ["plain", "compressed-archive", "compacted-compressed-archive"]
)
def test_native_nested_lineage_materializes_frozen_prefixes_and_resumes(
    representation, tmp_path, monkeypatch
):
    class SyntheticWriter(CodexReader):
        ALLOWED = CodexReader.ALLOWED | {"thread/resume", "thread/fork"}

    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream() as (provider, received, _), AdapterRuntime(provider) as runtime:
        parent_id = launch(tmp_path, runtime)[0]["thread_id"]
        compacted = representation.startswith("compacted")
        compacted_text = "  Synthetic compacted 🌙\n"
        if compacted:
            parent = find_session_by_id_or_title(parent_id).require_rollout()
            parent_records = [json.loads(raw) for raw in parent.read_bytes().splitlines()]
            checkpoint = {
                "timestamp": "2026-01-01T00:00:00Z",
                "ordinal": parent_records[-1]["ordinal"] + 1,
                "type": "compacted",
                "payload": {
                    "message": compacted_text,
                    "replacement_history": [
                        {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "Synthetic frozen compacted context"}
                            ],
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": compacted_text}],
                        },
                    ],
                    "replacement_history_metadata": [{}, {}],
                    "window_number": 1,
                    "window_id": str(uuid4()),
                },
            }
            with parent.open("ab") as target:
                target.write(json.dumps(checkpoint, ensure_ascii=False).encode() + b"\n")
        environment = os.environ.copy()
        environment["CMGR_NATIVE_FIXTURE_KEY"] = runtime.token

        def fork(identity):
            with SyntheticWriter(environment=environment) as writer:
                writer.request("thread/resume", {"threadId": identity})
                return writer.request("thread/fork", {"threadId": identity, "excludeTurns": True})[
                    "thread"
                ]["id"]

        child_id = fork(parent_id)
        launch(tmp_path, runtime, session=child_id)
        root_id = fork(child_id)
        # Both ancestors now get a newer turn excluded by the grandchild's frozen cutoffs.
        launch(tmp_path, runtime, session=parent_id)
        launch(tmp_path, runtime, session=child_id)
        root = find_session_by_id_or_title(root_id).require_rollout()
        lineage = resolve_history_lineage(root)
        assert len(lineage.segments) == 3
        with SyntheticWriter(environment=environment) as writer:
            writer.request("thread/resume", {"threadId": parent_id})
            with (
                pytest.raises(FileConflictError),
                session_write_guards(
                    (segment.metadata.id, segment.path) for segment in lineage.segments
                ),
            ):
                pytest.fail("An active native ancestor must prevent coordinated publication.")
            with prepare_history_copy(root, "fixture") as prepared:
                with pytest.raises(FileConflictError):
                    prepared.publish()
                assert not prepared.target.exists()
        if representation.endswith("compressed-archive"):
            for index, segment in enumerate(lineage.segments[:-1]):
                source = segment.path
                destination = source.with_suffix(".jsonl.zst")
                if index == 0:
                    folder = get_codex_home() / "archived_sessions"
                    folder.mkdir(exist_ok=True)
                    destination = folder / destination.name
                destination.write_bytes(
                    zstandard.ZstdCompressor(write_checksum=True).compress(source.read_bytes())
                )
                source.unlink()
            lineage = resolve_history_lineage(root)
            assert lineage.segments[0].path.parent.name == "archived_sessions"
            assert all(segment.path.name.endswith(".zst") for segment in lineage.segments[:-1])
        originals = {segment.path: segment.path.read_bytes() for segment in lineage.segments}
        expected = lineage.fingerprint()
        with session_write_guards(
            (segment.metadata.id, segment.path) for segment in lineage.segments
        ):
            assert lineage.fingerprint() == expected
            records = list(lineage.iter_records())
        with prepare_history_copy(root, "fixture") as prepared:
            assert prepared.expected_fingerprint == expected
            imported_id = prepared.id
            imported_bytes = prepared.candidate.read_bytes()
            assert imported_bytes.split(b"\n", 1)[1] == b"".join(records[1:])
            imported = prepared.publish()
        assert read_history_copy(imported_id).state == "published"
        resumed = launch(tmp_path, runtime, session=imported_id)
        assert resumed[0]["thread_id"] == imported_id
        assert {path: path.read_bytes() for path in originals} == originals
        assert imported.read_bytes().startswith(imported_bytes)
        assert recover_history_copy(imported_id).target == imported
        assert read_history_copy(imported_id).state == "published"
        prior = [
            message for message in received[-1][2]["messages"] if message["role"] == "assistant"
        ]
        assert len(prior) == 2
        expected_texts = [compacted_text if compacted else "  Exact 🌙\n", "  Exact 🌙\n"]
        assert [message["content"] for message in prior] == [
            [{"type": "text", "text": text}] for text in expected_texts
        ]


_NATIVE_RUNNER = """
import json,subprocess,sys
with open(sys.argv[2], 'w', encoding='utf-8') as output, open(sys.argv[3], 'w', encoding='utf-8') as errors:
    result = subprocess.run(json.loads(sys.argv[1]), stdin=subprocess.DEVNULL,
                            stdout=output, stderr=errors, check=False, timeout=45)
print(json.dumps({'returncode': result.returncode}), flush=True)
"""


def launch(tmp_path, runtime, *, session=None, writable=False, reasoning=False):
    home = get_codex_home()
    home.mkdir(parents=True, exist_ok=True)
    work = tmp_path / "workspace"
    work.mkdir(exist_ok=True)
    catalog = catalog_document(
        ProviderConfig(
            id="fixture",
            name="Synthetic",
            base_url=runtime.base_url,
            models=[ModelInfo(id="synthetic/exact:model", name="Synthetic model")],
        ),
        instructions="Use the declared coding tools and preserve the supplied text.",
    )
    catalog["models"][0]["apply_patch_tool_type"] = "freeform"
    if reasoning:
        catalog["models"][0]["supported_reasoning_levels"] = [
            {"effort": "medium", "description": "Synthetic"}
        ]
        catalog["models"][0]["supports_reasoning_summary_parameter"] = True
    catalog_path = home / "synthetic-models.json"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    config = {
        "model": "synthetic/exact:model",
        "model_provider": "fixture",
        "model_reasoning_effort": "medium" if reasoning else "none",
        "model_reasoning_summary": "none",
        "web_search": "disabled",
        "cli_auth_credentials_store": "file",
        "projects": {str(work): {"trust_level": "trusted"}},
        "model_catalog_json": str(catalog_path),
        "features": {
            "plugins": False,
            "recommended_plugins": False,
            "remote_plugin": False,
            "remote_models": False,
        },
        "model_providers": {
            "fixture": {
                "name": "Synthetic",
                "base_url": runtime.base_url,
                "env_key": "CMGR_NATIVE_FIXTURE_KEY",
                "wire_api": "responses",
                "requires_openai_auth": False,
                "supports_websockets": False,
                "request_max_retries": 0,
                "stream_max_retries": 0,
            }
        },
    }
    (home / "config.toml").write_text(tomlkit.dumps(config), encoding="utf-8")
    binary = find_codex_binary()
    assert binary is not None, "Install pinned Codex to run native adapter contracts."
    command = [
        str(binary),
        "exec",
        "--json",
        "--skip-git-repo-check",
        "--ignore-rules",
        "--sandbox",
        # A fixed synthetic patch tests transport independently of OS sandbox setup.
        # Product launch inherits the user's native permissions without this override.
        "danger-full-access" if writable else "read-only",
        "-C",
        str(work),
    ]
    if session is not None:
        command += ["resume", session]
    command += ["--", "Use the synthetic provider response and report its exact text."]
    environment = os.environ.copy()
    environment["CMGR_NATIVE_FIXTURE_KEY"] = runtime.token
    return _execute_native(tmp_path, command, environment, session=session)


def _execute_native(tmp_path, command, environment, *, session=None):
    output_path = tmp_path / ("resume.jsonl" if session else "fresh.jsonl")
    errors_path = tmp_path / "stderr.txt"
    owner = OwnedBackgroundProcess(
        [
            sys.executable,
            "-I",
            "-S",
            "-c",
            _NATIVE_RUNNER,
            json.dumps(command),
            str(output_path),
            str(errors_path),
        ],
        env=environment,
    )
    try:
        assert owner._done.wait(50), "Native synthetic adapter execution timed out."
    finally:
        owner.close()
    with owner.process.stdout as output:
        result = json.loads(output.read(100))
    events = [
        json.loads(line)
        for line in output_path.read_text(encoding="utf-8").splitlines()
        if line.startswith("{")
    ]
    assert result["returncode"] == 0, "Native synthetic adapter execution did not complete."
    assert events[-1]["type"] == "turn.completed"
    return events


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_native_verified_launch_overrides_preserve_original_configuration(
    protocol, tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    home = get_codex_home()
    home.mkdir()
    work = tmp_path / "workspace"
    work.mkdir()
    config = home / "config.toml"
    config.write_text(
        tomlkit.dumps(
            {
                "model": "synthetic-original",
                "cli_auth_credentials_store": "file",
                "projects": {str(work): {"trust_level": "trusted"}},
                "features": {
                    "plugins": False,
                    "recommended_plugins": False,
                    "remote_plugin": False,
                    "remote_models": False,
                },
            }
        ),
        encoding="utf-8",
    )
    original = config.read_bytes()
    binary = find_codex_binary()
    assert binary is not None
    with upstream(protocol) as (provider, received, _):
        identity = None
        for session in (None, "resume"):
            with AdapterLaunch(provider, cwd=work) as scope:
                command = [
                    str(binary),
                    "exec",
                    "--json",
                    "--skip-git-repo-check",
                    "--ignore-rules",
                    "--sandbox",
                    "read-only",
                    "-C",
                    str(work),
                ]
                if session:
                    assert identity is not None
                    command.extend(["resume", identity])
                # Global config overrides must also be accepted after the resume ID.
                command.extend(scope.arguments)
                command.extend(
                    ["--", "Use the synthetic provider response and report its exact text."]
                )
                events = _execute_native(tmp_path, command, scope.environment, session=session)
                if not session:
                    identity = events[0]["thread_id"]
                assert events[-2]["item"]["text"] == "  Exact 🌙\n"
                assert config.read_bytes() == original
                rollouts = list((home / "sessions").rglob("*.jsonl"))
                assert len(rollouts) == 1
                header = json.loads(rollouts[0].read_text(encoding="utf-8").splitlines()[0])
                assert header["payload"]["model_provider"] == adapter_connection_id(provider)
        assert len(received) == 2
        assert all(request[2]["model"] == provider.default_model for request in received)
        assert config.read_bytes() == original
        assert not (home / "cmgr.json").exists()


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_native_exact_text_response_through_owned_adapter(protocol, tmp_path, monkeypatch):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream(protocol) as (provider, received, _), AdapterRuntime(provider) as runtime:
        events = launch(tmp_path, runtime)
        messages = [
            event["item"]
            for event in events
            if event["type"] == "item.completed" and event["item"].get("type") == "agent_message"
        ]
        assert messages[-1]["text"] == "  Exact 🌙\n"
        assert len(received) == 1
        assert received[0][2]["model"] == "synthetic/exact:model"


@pytest.mark.parametrize("protocol", ["chat", "messages"])
@pytest.mark.parametrize("writable", [False, True])
def test_native_custom_patch_result_and_resume_round_trip(
    protocol, writable, tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    patch = "*** Begin Patch\n*** Add File: synthetic.txt\n+Exact synthetic file.\n*** End Patch\n"
    replies = []

    def respond(body):
        replies.append(body)
        if len(replies) == 1:
            definitions = body["tools"]
            name = (
                next(
                    (
                        tool["function"]["name"]
                        for tool in definitions
                        if tool.get("function", {}).get("name") == "apply_patch"
                    ),
                    None,
                )
                if protocol == "chat"
                else next(
                    (tool["name"] for tool in definitions if tool.get("name") == "apply_patch"),
                    None,
                )
            )
            if name is None:
                return b'data: {"error":{"message":"Synthetic patch tool unavailable."}}\n\n'
            arguments = json.dumps({"input": patch})
            if protocol == "chat":
                source = [
                    chat(
                        {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "synthetic-patch-call",
                                    "type": "function",
                                    "function": {"name": name, "arguments": arguments},
                                }
                            ]
                        }
                    ),
                    chat(finish="tool_calls"),
                    "[DONE]",
                ]
            else:
                source = [
                    start(),
                    block(
                        0,
                        {
                            "type": "tool_use",
                            "id": "synthetic-patch-call",
                            "name": name,
                            "input": {},
                        },
                    ),
                    delta(0, arguments, tool=True),
                    stop(0),
                    finish("tool_use"),
                    {"type": "message_stop"},
                ]
        elif protocol == "chat":
            source = [chat({"content": "SYNTHETIC_COMPLETE"}), chat(finish="stop"), "[DONE]"]
        else:
            source = [
                start(),
                block(0, {"type": "text", "text": "SYNTHETIC_COMPLETE"}),
                stop(0),
                finish(),
                {"type": "message_stop"},
            ]
        return wire(source, protocol)[0]

    with (
        upstream(protocol, responder=respond) as (provider, _, _),
        AdapterRuntime(provider) as runtime,
    ):
        events = launch(tmp_path, runtime, writable=writable)
        path = tmp_path / "workspace/synthetic.txt"
        if writable:
            assert path.read_bytes() == b"Exact synthetic file.\n"
            changes = [
                event["item"]
                for event in events
                if event["type"] == "item.completed" and event["item"].get("type") == "file_change"
            ]
            assert changes and changes[-1]["status"] == "completed"
        else:
            assert not path.exists()
        assert len(replies) == 2
        session = next(event["thread_id"] for event in events if event["type"] == "thread.started")
        resumed = launch(tmp_path, runtime, session=session, writable=writable)
        assert resumed[-1]["type"] == "turn.completed" and len(replies) == 3
    for body in replies[1:]:
        if protocol == "chat":
            results = [item for item in body["messages"] if item["role"] == "tool"]
            assert results and results[-1]["tool_call_id"] == "synthetic-patch-call"
        else:
            results = [
                item
                for message in body["messages"]
                for item in message["content"]
                if item["type"] == "tool_result"
            ]
            assert results and results[-1]["tool_use_id"] == "synthetic-patch-call"
        if not writable:
            output = results[-1]["content"]
            if isinstance(output, list):
                output = "".join(item["text"] for item in output if item["type"] == "text")
            assert "read-only sandbox" in output


def test_native_signed_thinking_survives_tool_result_owner_restart_and_resume(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    signed = {
        "type": "thinking",
        "thinking": "Exact synthetic thinking.",
        "signature": "synthetic-signature",
    }
    redacted = {"type": "redacted_thinking", "data": "synthetic-redacted"}
    replies = []

    def respond(body):
        replies.append(body)
        if len(replies) == 1:
            patch = (
                "*** Begin Patch\n*** Add File: signed.txt\n+Exact signed file.\n*** End Patch\n"
            )
            source = [
                start(),
                block(0, signed),
                stop(0),
                block(1, redacted),
                stop(1),
                block(
                    2,
                    {
                        "type": "tool_use",
                        "id": "signed-patch-call",
                        "name": "apply_patch",
                        "input": {"input": patch},
                    },
                ),
                stop(2),
                finish("tool_use"),
                {"type": "message_stop"},
            ]
        else:
            source = [
                start(),
                block(0, {"type": "text", "text": "SYNTHETIC_COMPLETE"}),
                stop(0),
                finish(),
                {"type": "message_stop"},
            ]
        return wire(source, "messages")[0]

    with upstream("messages", responder=respond) as (provider, _, _):
        provider.reasoning_budget = 1024
        with AdapterRuntime(provider) as runtime:
            events = launch(tmp_path, runtime, writable=True, reasoning=True)
            session = next(
                event["thread_id"] for event in events if event["type"] == "thread.started"
            )
        assert (tmp_path / "workspace/signed.txt").read_bytes() == b"Exact signed file.\n"
        with AdapterRuntime(provider) as restarted:
            launch(tmp_path, restarted, session=session, writable=True, reasoning=True)
    assert len(replies) == 3
    for body in replies[1:]:
        blocks = [item for message in body["messages"] for item in message["content"]]
        assert [item for item in blocks if item["type"] in {"thinking", "redacted_thinking"}] == [
            signed,
            redacted,
        ]
        assert any(item.get("tool_use_id") == "signed-patch-call" for item in blocks)
