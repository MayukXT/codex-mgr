"""Owned loopback transport contracts against synthetic local HTTP servers."""

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import pytest
from test_continuations import SIGNED, thinking_stream
from test_protocol_codecs import messages_reply
from test_streaming_codecs import request, text_stream, wire

from codex_mgr.config.schema import ProviderConfig
from codex_mgr.core.adapters import runtime as runtime_module
from codex_mgr.core.adapters.runtime import AdapterError, AdapterRuntime
from codex_mgr.core.adapters.sse import json_events
from codex_mgr.core.adapters.streaming import async_adapt_stream

_SYNC_TRANSPORT = httpx.HTTPTransport.handle_request
_ASYNC_TRANSPORT = httpx.AsyncHTTPTransport.handle_async_request
_PORTS = set()


@pytest.fixture(autouse=True)
def synthetic_loopback_http(isolated_codex_home, monkeypatch):
    """Only permit exact local ports bound by this test's synthetic servers."""
    _PORTS.clear()

    def allow_sync(transport, req):
        assert req.url.scheme == "http" and req.url.host == "127.0.0.1" and req.url.port in _PORTS
        return _SYNC_TRANSPORT(transport, req)

    async def allow_async(transport, req):
        assert req.url.scheme == "http" and req.url.host == "127.0.0.1" and req.url.port in _PORTS
        return await _ASYNC_TRANSPORT(transport, req)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", allow_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", allow_async)


@contextmanager
def upstream(
    protocol="chat",
    *,
    status=200,
    idle=False,
    redirect=False,
    raw=None,
    buffered=False,
    responder=None,
):
    received, disconnected = [], threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.append((self.path, dict(self.headers), json.loads(body)))
            self.send_response(302 if redirect else status)
            if redirect:
                self.send_header("Location", "http://127.0.0.1:1/private-redirect")
            self.send_header("Connection", "close")
            self.send_header(
                "Content-Type",
                "application/json" if buffered or status != 200 else "text/event-stream",
            )
            self.end_headers()
            self.wfile.flush()
            if idle:
                self.connection.settimeout(3)
                try:
                    if self.connection.recv(1) == b"":
                        disconnected.set()
                except ConnectionResetError:
                    disconnected.set()
                except OSError:
                    pass
            else:
                if responder is not None:
                    data = responder(received[-1][2])
                elif raw is not None:
                    data = raw
                elif buffered:
                    data = json.dumps(
                        {
                            "model": "synthetic/exact:model",
                            "choices": [
                                {
                                    "message": {
                                        "role": "assistant",
                                        "content": "Exact buffered.\n",
                                    },
                                    "finish_reason": "stop",
                                }
                            ],
                        }
                    ).encode()
                elif status != 200:
                    data = b'{"error":{"message":"synthetic-private-error"}}'
                else:
                    data = wire(text_stream(protocol), protocol)[0]
                try:
                    self.wfile.write(data)
                    self.wfile.flush()
                except OSError:
                    pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    _PORTS.add(server.server_port)
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    provider = ProviderConfig(
        id="synthetic",
        name="Synthetic",
        default_model="synthetic/exact:model",
        base_url=f"http://127.0.0.1:{server.server_port}/v1",
        wire_api="anthropic_proxy" if protocol == "messages" else "chat",
        auth_type="env_var",
        auth_token="$SYNTHETIC_UPSTREAM_KEY",
    )
    try:
        yield provider, received, disconnected
    finally:
        server.shutdown()
        server.server_close()
        worker.join(3)
        assert not worker.is_alive()


def payload(*, stream=True):
    return {
        "model": "synthetic/exact:model",
        "input": "  Exact synthetic prompt.\n",
        "stream": stream,
    }


def headers(runtime):
    _PORTS.add(int(runtime.base_url.split(":")[2].split("/")[0]))
    return {"Authorization": "Bearer " + runtime.token}


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_actual_loopback_translates_stream_and_separates_owner_and_upstream_credentials(
    protocol, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-private-upstream-key")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    with upstream(protocol) as (provider, received, _), AdapterRuntime(provider) as runtime:
        token = runtime.token
        assert token not in repr(runtime) and "synthetic-private-upstream-key" not in repr(runtime)
        response = httpx.post(
            runtime.base_url + "/responses",
            headers=headers(runtime),
            json=payload(),
            trust_env=False,
        )
        assert response.status_code == 200
        events = [event.data for event in json_events([response.content])]
        assert events[-1]["type"] == "response.completed"
        assert events[-1]["response"]["output"][0]["content"][0]["text"] == "  Exact 🌙\n"
        path, outgoing, body = received[0]
        assert path == ("/v1/messages" if protocol == "messages" else "/v1/chat/completions")
        credential = outgoing["x-api-key" if protocol == "messages" else "Authorization"]
        assert "synthetic-private-upstream-key" in credential and token not in json.dumps(outgoing)
        assert body["messages"][-1]["content"][0]["text"] == "  Exact synthetic prompt.\n"
        assert body["model"] == payload()["model"] and body["stream"] is True
    assert not runtime._thread.is_alive()
    with pytest.raises(AdapterError, match="not running"):
        _ = runtime.token
    runtime.close()


def test_buffered_reply_is_validated_before_publication(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream(buffered=True) as (provider, _, _), AdapterRuntime(provider) as runtime:
        response = httpx.post(
            runtime.base_url + "/responses",
            headers=headers(runtime),
            json=payload(stream=False),
            trust_env=False,
        )
        assert response.status_code == 200
        assert response.json()["output"][0]["content"][0]["text"] == "Exact buffered.\n"


def test_disabled_upstream_streaming_keeps_native_stream_contract(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream(buffered=True) as (provider, received, _):
        provider.streaming = False
        with AdapterRuntime(provider) as runtime:
            response = httpx.post(
                runtime.base_url + "/responses",
                headers=headers(runtime),
                json=payload(),
                trust_env=False,
            )
            assert response.status_code == 200
            events = [event.data for event in json_events([response.content])]
            assert events[-1]["type"] == "response.completed"
            assert events[-1]["response"]["output"][0]["content"][0]["text"] == "Exact buffered.\n"
            assert received[0][2]["stream"] is False and "stream_options" not in received[0][2]


@pytest.mark.parametrize("saved", ["none", "auto"])
@pytest.mark.parametrize("supplied", ["missing", "null", "none", "auto"])
@pytest.mark.parametrize("buffered", [False, True])
def test_saved_messages_summary_applies_when_native_omits_it(
    saved, supplied, buffered, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    body = messages_reply([SIGNED, {"type": "text", "text": "Exact response."}])
    reply = json.dumps(body).encode() if buffered else wire(thinking_stream(), "messages")[0]
    with upstream("messages", buffered=buffered, raw=reply) as (provider, received, _):
        provider.reasoning_budget = 1024
        provider.messages_summary = saved
        provider.streaming = not buffered
        selected = {**payload(), "reasoning": {"effort": "medium"}}
        if supplied != "missing":
            selected["reasoning"]["summary"] = None if supplied == "null" else supplied
        with AdapterRuntime(provider) as runtime:
            response = httpx.post(
                runtime.base_url + "/responses",
                headers=headers(runtime),
                json=selected,
                trust_env=False,
            )
            assert response.status_code == 200
            events = [event.data for event in json_events([response.content])]
            assert events[-1]["type"] == "response.completed"
            chosen = saved if supplied in {"missing", "null"} else supplied
            assert received[0][2]["thinking"]["display"] == (
                "summarized" if chosen == "auto" else "omitted"
            )
            expected = (
                [{"type": "summary_text", "text": SIGNED["thinking"]}] if chosen == "auto" else []
            )
            assert events[-1]["response"]["output"][0]["summary"] == expected
            if chosen == "none":
                assert SIGNED["thinking"] not in str(events)
            assert SIGNED["signature"] not in str(events)


@pytest.mark.parametrize("status", [401, 403, 429, 500])
def test_upstream_rejections_are_non_successful_and_do_not_expose_private_errors(
    status, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream(status=status) as (provider, _, _), AdapterRuntime(provider) as runtime:
        response = httpx.post(
            runtime.base_url + "/responses",
            headers=headers(runtime),
            json=payload(),
            trust_env=False,
        )
        assert response.status_code == (status if status in {401, 403, 429} else 502)
        assert "synthetic-private-error" not in response.text


def test_upstream_redirect_is_not_followed(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream(redirect=True) as (provider, received, _), AdapterRuntime(provider) as runtime:
        response = httpx.post(
            runtime.base_url + "/responses",
            headers=headers(runtime),
            json=payload(),
            trust_env=False,
        )
        assert response.status_code == 502 and len(received) == 1
        assert "private-redirect" not in response.text


def send(runtime, *, method="POST", path="/v1/responses", overrides=None, extra="", body=b"{}"):
    port = int(runtime.base_url.split(":")[2].split("/")[0])
    values = {
        "Host": f"127.0.0.1:{port}",
        "Authorization": "Bearer " + runtime.token,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }
    values.update(overrides or {})
    raw = (
        f"{method} {path} HTTP/1.1\r\n"
        + "".join(f"{key}: {value}\r\n" for key, value in values.items())
        + extra
        + "\r\n"
    ).encode() + body
    with socket.create_connection(("127.0.0.1", port), timeout=3) as connection:
        connection.sendall(raw)
        parts = []
        while part := connection.recv(4096):
            parts.append(part)
    return b"".join(parts)


@pytest.mark.parametrize(
    "options,status",
    [
        ({"overrides": {"Authorization": "Bearer wrong"}}, 401),
        ({"overrides": {"Host": "evil.invalid"}}, 403),
        ({"extra": "Origin: https://evil.invalid\r\n"}, 403),
        ({"method": "GET"}, 404),
        ({"path": "/health"}, 404),
        ({"path": "/v1/responses?redirect=other"}, 404),
        ({"extra": "Authorization: Bearer duplicate\r\n"}, 400),
        ({"extra": "Transfer-Encoding: chunked\r\n"}, 400),
        ({"extra": "Content-Encoding: gzip\r\n"}, 400),
        ({"overrides": {"Content-Type": "text/plain"}}, 415),
        ({"overrides": {"Content-Length": "-1"}}, 400),
        ({"overrides": {"Content-Length": "8388609"}}, 413),
        ({"body": b'{"model":"synthetic-private-marker","model":"duplicate"}'}, 400),
    ],
)
def test_exact_methods_paths_authorization_headers_and_body_bound_before_any_upstream_request(
    options, status, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream() as (provider, received, _), AdapterRuntime(provider) as runtime:
        response = send(runtime, **options)
        assert response.startswith(f"HTTP/1.1 {status}".encode())
        assert "synthetic-private-marker" not in response.decode()
        assert received == []


@pytest.mark.parametrize("closing_owner", [False, True])
def test_disconnect_or_owner_close_cancels_an_idle_upstream_without_waiting_for_read_timeout(
    closing_owner, monkeypatch
):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with upstream(idle=True) as (provider, _, disconnected):
        with (
            AdapterRuntime(provider, idle_timeout=60) as runtime,
            httpx.Client(trust_env=False, timeout=3) as client,
            client.stream(
                "POST", runtime.base_url + "/responses", headers=headers(runtime), json=payload()
            ) as response,
        ):
            assert response.status_code == 200
            lines = response.iter_lines()
            assert next(lines) == "event: response.created"
            begun = time.monotonic()
            if closing_owner:
                runtime.close()
                assert not runtime._startup_failed
                assert time.monotonic() - begun < 1, (
                    "Owner close waited for the synthetic upstream timeout."
                )
            else:
                response.close()
            assert disconnected.wait(2), "An idle upstream remained open after its owner ended."
            assert time.monotonic() - begun < 3
        assert not runtime._thread.is_alive()


def test_capacity_bound_and_request_header_timeout_release_owned_tasks(monkeypatch):
    monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
    with (
        upstream() as (provider, _, _),
        AdapterRuntime(provider, max_active=1, request_timeout=0.1) as runtime,
    ):
        port = int(runtime.base_url.split(":")[2].split("/")[0])
        with socket.create_connection(("127.0.0.1", port), timeout=2) as first:
            first.sendall(b"POST /v1/responses HTTP/1.1\r\n")
            # First response proves the request was accepted and its timeout is bounded.
            assert first.recv(4096).startswith(b"HTTP/1.1 400")
        assert len(runtime._tasks) <= 1


@pytest.mark.parametrize("boundary", ["headers", "body", "drain"])
def test_owner_cancellation_survives_an_io_operation_completing_in_the_same_turn(boundary):
    provider = ProviderConfig(
        id="synthetic",
        name="Synthetic",
        base_url="http://127.0.0.1:1/v1",
        wire_api="chat",
        auth_type="none",
    )
    runtime = AdapterRuntime(provider)
    runtime._base_url = "http://127.0.0.1:1/v1"
    body = json.dumps(payload(stream=False)).encode()

    async def exercise():
        owner = asyncio.current_task()

        class Reader:
            async def readuntil(self, _):
                if boundary == "headers":
                    owner.cancel()
                return (
                    "POST /v1/responses HTTP/1.1\r\n"
                    "Host: 127.0.0.1:1\r\n"
                    f"Authorization: Bearer {runtime._token}\r\n"
                    "Content-Type: application/json\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode()

            async def readexactly(self, _):
                if boundary == "body":
                    owner.cancel()
                return body

        class Writer:
            def write(self, _):
                pass

            async def drain(self):
                owner.cancel()

        if boundary == "drain":
            await runtime._write(Writer(), b"synthetic")
        else:
            await runtime._request(Reader())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(exercise())


def test_legacy_asyncio_timeout_class_is_handled_without_builtin_alias(monkeypatch):
    class LegacyTimeout(Exception):
        pass

    async def timed_out(awaitable, *_):
        awaitable.close()
        raise LegacyTimeout

    class Reader:
        async def readuntil(self, _):
            return b""

    monkeypatch.setattr(asyncio, "TimeoutError", LegacyTimeout)
    monkeypatch.setattr(runtime_module, "_bounded", timed_out)
    provider = ProviderConfig(
        id="synthetic",
        name="Synthetic",
        base_url="http://127.0.0.1:1/v1",
        wire_api="chat",
        auth_type="none",
    )
    runtime = AdapterRuntime(provider)
    with pytest.raises(runtime_module._RequestError) as failure:
        asyncio.run(runtime._request(Reader()))
    assert failure.value.status == 400


def test_credentials_and_transport_controls_fail_without_leaking_or_starting_a_listener(
    monkeypatch,
):
    with upstream() as (provider, _, _):
        monkeypatch.delenv("SYNTHETIC_UPSTREAM_KEY", raising=False)
        with pytest.raises(AdapterError, match="credential") as error:
            AdapterRuntime(provider)
        assert "SYNTHETIC_UPSTREAM_KEY" not in str(error.value)
        monkeypatch.setenv("SYNTHETIC_UPSTREAM_KEY", "synthetic-key")
        provider.http_headers = {"Host": "private.invalid"}
        with pytest.raises(AdapterError, match="conflicts"):
            AdapterRuntime(provider)
        provider.http_headers = None
        for limit in (0, True, 33):
            with pytest.raises(AdapterError, match="limits"):
                AdapterRuntime(provider, max_active=limit)
        provider.auth_type = "none"
        with AdapterRuntime(provider) as runtime:
            assert "Authorization" not in runtime._headers


@pytest.mark.parametrize("protocol", ["chat", "messages"])
def test_async_codec_has_the_same_incremental_utf8_and_terminal_contract(protocol):
    async def chunks():
        for part in wire(text_stream(protocol), protocol, chunk_size=1):
            yield part

    async def consume():
        return [event async for event in async_adapt_stream(chunks(), request(protocol))]

    events = asyncio.run(consume())
    assert events[-1]["type"] == "response.completed"
    assert (
        "".join(event["delta"] for event in events if event["type"] == "response.output_text.delta")
        == "  Exact 🌙\n"
    )
