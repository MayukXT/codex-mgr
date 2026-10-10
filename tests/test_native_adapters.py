"""Opt-in native Codex requests against synthetic loopback protocol providers."""

import json
import os
import sys

import pytest
import tomlkit
from test_adapter_runtime import synthetic_loopback_http, upstream  # noqa: F401 -- shared fixture
from test_streaming_codecs import block, chat, delta, finish, start, stop, wire

from codex_mgr.config.schema import ModelInfo, ProviderConfig
from codex_mgr.core.adapters.launch import AdapterLaunch, adapter_connection_id
from codex_mgr.core.adapters.runtime import AdapterRuntime
from codex_mgr.core.catalog import catalog_document
from codex_mgr.utils.paths import find_codex_binary, get_codex_home
from codex_mgr.utils.processes import OwnedBackgroundProcess

pytestmark = pytest.mark.skipif(
    os.environ.get("CMGR_NATIVE_CONTRACTS") != "1",
    reason="Set CMGR_NATIVE_CONTRACTS=1 with Codex 0.159.0 installed.",
)

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
